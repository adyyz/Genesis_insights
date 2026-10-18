"""Sequential persisted triage, inline when closed and in a separate opt-in worker."""
import argparse
import json
from time import monotonic, sleep

from .dataset import content_hash, stable_id
from .engine import AUTO_LABEL_VERSION, WEIGHTS, auto_label, enrich, prioritize, risk_assessment
from .gateway import GatewayResult, generate_json
from .models import StepRun, retry_allowed
from .prompts import CONTEXT_POLICY, LEGS, context_for, evolution_prompt_context
from .repository import Conflict, Repository, now


def _leg_profile(repo, bundle_id, leg):
    from .agent_profiles import frozen_profile
    return frozen_profile(repo.get("config_versions", bundle_id), leg, repo.settings.active_model_id)


def triage_leg(repo, child, step, evidence, risk, previous):
    if not repo.settings.ai_authorized(): return GatewayResult()
    bundle = repo.get("config_versions", child["config_bundle_id"])
    target_model = step.model_id or repo.settings.active_model_id
    bundle_body = (bundle or {}).get("body", {})
    try: profile = _leg_profile(repo, child["config_bundle_id"], step.leg_id)
    except ValueError: return GatewayResult(error_code="CONFIG_GENERATION_MISMATCH")
    if not bundle or profile.model_id != target_model:
        return GatewayResult(error_code="CONFIG_MISMATCH")
    if repo.settings.mode == "cloud" and (bundle_body.get("provider") != "GOOGLE_CLOUD"
        or bundle_body.get("location") != repo.settings.verified_cloud_location): return GatewayResult(error_code="CONFIG_MISMATCH")
    components = bundle["component_ids"]
    prompt = repo.get("config_versions", components.get(f"PROMPT_{step.leg_id}", "missing"))
    context = repo.get("config_versions", components.get("CONTEXT", "missing"))
    policy = repo.get("config_versions", components.get("RISK_POLICY", "missing"))
    schema, input_cap, output_cap, _instructions = LEGS[step.leg_id]
    if (not prompt or prompt["body"].get("response_schema") != schema.model_json_schema()
        or prompt["body"].get("input_cap") != input_cap or prompt["body"].get("output_cap") != output_cap
        or not context or context["body"] != CONTEXT_POLICY or not policy or policy["body"] != {"weights": WEIGHTS}):
        return GatewayResult(error_code="CONFIG_MISMATCH")
    selected = context_for(step.leg_id, evidence, risk, previous)
    text = prompt["body"]["instructions"] + "\nSOURCE_DATA:\n" + json.dumps(selected, sort_keys=True, separators=(",", ":"))
    step = step.model_copy(update={"prompt_version_id": prompt["id"], "context_version_id": context["id"],
        "risk_version_id": policy["id"], "model_id": target_model})
    return generate_json(repo, step, text, schema, input_limit=input_cap, output_limit=profile.max_output_tokens or output_cap, evidence={**evidence, "intelligence_version": 2},
                         model_id=target_model, thinking_level=profile.thinking_level, temperature=profile.temperature,
                         structured_output=step.leg_id in bundle_body.get("structured_output_legs", []))



def _update(repo, run_id, changes, lease=None, *, alert_state=None, finishing=False):
    if lease:
        return repo.worker_update(lease, run_id, changes, alert_state=alert_state, finishing=finishing)
    repo.db.collection("pipeline_runs").document(run_id).update(changes)
    if alert_state:
        run = repo.get("pipeline_runs", run_id)
        repo.db.collection("alerts").document(run["alert_id"]).update({"status": alert_state})


def _create_step(repo, step, lease=None):
    if lease: return repo.create_worker_step(lease, step)
    repo.create("step_runs", step.id, step.model_dump())
    return repo.get("step_runs", step.id)


def _checkpoint(repo, step, lease=None):
    row = repo.get("step_runs", step.id)
    if not row: return None
    if any(row.get(key) != getattr(step, key) for key in
        ("run_id", "dataset_id", "config_bundle_id", "alert_id", "case_id", "leg_id", "parent_step_ids", "attempt_number", "retry_of_step_id")):
        raise Conflict("CHECKPOINT_LINK_INVALID")
    if row["state"] == "STARTED":
        if lease and (row.get("worker_lease") or {}).get("owner") != lease.owner:
            return repo.recover_started_attempt(lease, step.id)
        original = StepRun.model_validate({key: value for key, value in row.items() if key in StepRun.model_fields})
        repo.seal_provider_attempt(original, {"state": "UNKNOWN_OUTCOME", "output": None,
            "execution_provenance": "UNKNOWN", "finished_at": now(), "error_code": "PROVIDER_OUTCOME_UNKNOWN"})
        return repo.get("step_runs", step.id)
    return row


def _model_attempt(repo, step, call, lease=None):
    row = _checkpoint(repo, step, lease)
    if row is None:
        result = call(step)
        if result.error_code in {"WORKER_LEASE_LOST", "WORKER_EXECUTION_BOUND"}:
            raise Conflict(result.error_code)
        row = _checkpoint(repo, step, lease)
        if row is None:
            row = _create_step(repo, step.model_copy(update={"state": "FAILED", "finished_at": now(),
                "model_id": None, "usage_status": "NOT_APPLICABLE", "cost_status": "NOT_APPLICABLE",
                "context_precount_tokens": result.context_precount_tokens,
                "execution_provenance": "NOT_SENT", "error_code": result.error_code or "UNSEALED_PROVIDER_RESULT"}), lease)
    return row


def _model_step(repo, step, call, evidence, lease=None):
    row = _model_attempt(repo, step, call, lease)
    bundle = repo.get("config_versions", step.config_bundle_id) if retry_allowed(row) else None
    if retry_allowed(row) and (bundle or {}).get("body", {}).get("attempts_per_leg", 1) == 2:
        second = step.model_copy(update={"id": stable_id(step.run_id, step.leg_id, 2), "attempt_number": 2,
            "retry_of_step_id": step.id, "started_at": now()})
        # Respect the provider minimum, including after worker recovery. The
        # gateway and atomic reservation recheck controls/lease before sending.
        if not repo.get("step_runs", second.id): sleep(row["retry_delay_seconds"])
        row = _model_attempt(repo, second, call, lease)
    if row["state"] != "SUCCEEDED": return row, row.get("error_code") or "STEP_FAILED"
    if (row.get("execution_provenance") != "REAL_PROVIDER" or not row.get("validation", {}).get("schema_pass")
        or not row.get("validation", {}).get("evidence_pass") or row.get("output") is None):
        return row, "UNVALIDATED_PROVIDER_RESULT"
    # Revalidate persisted checkpoints with the same boundary used at generation.
    from .validation import validate_model_output
    try:
        parsed = LEGS[step.leg_id][0].model_validate(row["output"])
        validate_model_output(parsed, evidence)
    except ValueError: return row, "CHECKPOINT_OUTPUT_INVALID"
    return row, None


def _child(repo, child, data, lease=None):
    ident = child["id"]
    alert_state = "RUNNING" if lease or repo.settings.local_gemini_enabled else None
    _update(repo, ident, {"state": "RUNNING", "started_at": child.get("started_at") or now(), "stage": "A1"},
        lease, alert_state=alert_state)
    start, clock = now(), monotonic()
    evidence = enrich(data, data.cases[child["case_id"]])
    risk = risk_assessment(evidence)
    base = {"dataset_id": repo.dataset_id, "run_id": ident, "alert_id": child["alert_id"],
        "case_id": child["case_id"], "config_bundle_id": child["config_bundle_id"],
        "input_hash": content_hash(evidence), "selected_evidence_ids": evidence["evidence_ids"]}
    previous_id = stable_id(ident, "A1", 1)
    a1 = StepRun(id=previous_id, **base, leg_id="A1", state="SUCCEEDED", worker_lease=lease,
        started_at=start, finished_at=now(), duration_ms=int((monotonic() - clock) * 1000), output=evidence,
        validation={"schema_pass": True, "evidence_pass": True})
    sealed = _checkpoint(repo, a1, lease) or _create_step(repo, a1, lease)
    if (sealed["state"] != "SUCCEEDED" or sealed.get("input_hash") != content_hash(evidence)
        or sealed.get("output") != evidence or sealed.get("execution_provenance") != "DETERMINISTIC"):
        return "CHECKPOINT_EVIDENCE_INVALID"
    _update(repo, ident, {"checkpoint_step_id": previous_id}, lease)
    previous = {}
    for leg in ["A2", "A3", "A4"]:
        _update(repo, ident, {"stage": leg}, lease)
        profile = _leg_profile(repo, child["config_bundle_id"], leg)
        step = StepRun(id=stable_id(ident, leg, 1), **base, leg_id=leg, state="STARTED", started_at=now(),
            parent_step_ids=[previous_id], model_id=profile.model_id, worker_lease=lease,
            usage_status="UNKNOWN", cost_status="UNKNOWN", execution_provenance="UNKNOWN")
        sealed, error = _model_step(repo, step,
            lambda attempt: triage_leg(repo, child, attempt, evidence, risk, previous), evidence, lease)
        _update(repo, ident, {"checkpoint_step_id": sealed["id"]}, lease)
        if error: return error
        previous[leg] = sealed["output"]
        previous_id = sealed["id"]
    _update(repo, ident, {"stage": "A0"}, lease)
    priority = prioritize(evidence, risk, previous["A3"]["suggested_action"])
    bundle = repo.get("config_versions", child["config_bundle_id"])
    if bundle.get("body", {}).get("auto_label_version") == AUTO_LABEL_VERSION:
        from .calibration import cohort, BASELINE
        version = bundle["body"].get("auto_label_policy_version", BASELINE)
        thresholds = None
        if version != BASELINE:
            policy = repo.get("config_versions", version)
            if not policy or content_hash(policy["body"]) != version: return "A0_POLICY_INTEGRITY_FAILED"
            selected = policy["body"]["rules"][repo.get("alerts", child["alert_id"])["rule_id"]]
            if selected.get("cohort") == cohort(bundle): thresholds = selected
        priority.update(auto_label(evidence, previous["A3"], thresholds, version))
        if "auto_label_policy_version" in bundle["body"]:
            priority.update(thresholds=thresholds or {"tp": .25, "fp": .65},
                threshold_source="LEARNED" if thresholds else "BASELINE_FALLBACK", cohort=cohort(bundle))
    a0 = StepRun(id=stable_id(ident, "A0", 1), **base, leg_id="A0", state="SUCCEEDED", worker_lease=lease,
        started_at=now(), finished_at=now(), output=priority, parent_step_ids=[previous_id],
        validation={"schema_pass": True, "evidence_pass": True})
    sealed = _checkpoint(repo, a0, lease) or _create_step(repo, a0, lease)
    if sealed["state"] != "SUCCEEDED" or sealed.get("output") != priority:
        return "CHECKPOINT_PRIORITY_INVALID"
    _update(repo, ident, {"checkpoint_step_id": a0.id}, lease)
    repo.complete_alert(ident, lease=lease)
    return None


def _run_batch(repo, parent_id, lease=None):
    parent = repo.get("pipeline_runs", parent_id)
    try: data = repo.load_dataset()
    except Exception:
        if lease: raise  # Preserve ownership/checkpoints for bounded delivery recovery.
        data = None
    errors = []
    for child_id in parent["child_run_ids"]:
        child = repo.get("pipeline_runs", child_id)
        if child["state"] == "COMPLETE": continue
        if child["state"] == "FAILED":
            errors.append(child.get("error_code") or "CHILD_FAILED")
            continue
        try: error = _child(repo, child, data, lease) if data is not None else "SOURCE_UNAVAILABLE"
        except Conflict as exc:
            if lease and str(exc) in {"WORKER_LEASE_LOST", "WORKER_EXECUTION_BOUND", "ATTEMPT_STILL_OWNED"}: raise
            error = "RUN_CONFLICT"
        except Exception:
            if lease: raise
            error = "WORKER_FAILED"
        if error:
            errors.append(error)
            _update(repo, child_id, {"state": "FAILED", "completed_at": now(), "error_code": error}, lease,
                alert_state="ERROR" if lease or repo.settings.local_gemini_enabled else None, finishing=True)
            if not lease: repo.audit("FAILED", "pipeline_runs", child_id, "local-worker", run_id=child_id, reason=error)
    if lease: repo.finish_worker_batch(lease)
    else: _update(repo, parent_id, {"state": "FAILED" if errors else "COMPLETE",
        "completed_at": now(), "error_code": errors[0] if errors else None})


def run_local(repo, parent_id):
    """The API uses this only for refusal; live use belongs to the separate worker."""
    if not repo.claim_batch(parent_id): return False
    _run_batch(repo, parent_id)
    return True


def worker_once(repo):
    if repo.settings.mode != "local": raise RuntimeError("This worker supports only loopback emulators")
    if not repo.settings.local_gemini_enabled: raise RuntimeError("Opt in to local Gemini before starting this worker")
    from .bulk_jobs import advance, scoped
    from .bulk_dataset import ID as BULK_ID
    from .baseline_workspace import selected_id
    dataset_id = selected_id(repo) or BULK_ID
    for job in repo.page("bulk_jobs", 50, filters=[("state", "QUEUED"), ("dataset_id", dataset_id)])["items"]: advance(repo, job["id"])
    bulk_runs = repo.page("pipeline_runs", 100, filters=[("kind", "TRIAGE_BATCH"), ("state", "QUEUED"), ("dataset_id", dataset_id)])
    bulk_count = sum(run_local(scoped(repo, run), run["id"]) for run in bulk_runs["items"])
    # Seal each parent before advancing its case coordinator again.
    for job in repo.page("bulk_jobs", 50, filters=[("state", "QUEUED"), ("dataset_id", dataset_id)])["items"]: advance(repo, job["id"])
    bulk_evolution = repo.page("pipeline_runs", 100, filters=[("kind", "EVOLUTION"), ("state", "QUEUED"), ("dataset_id", dataset_id)])
    bulk_count += sum(run_evolution(scoped(repo, run), run["id"]) for run in bulk_evolution["items"])
    return bulk_count


def _evolution_leg(repo, run, step, context):
    if not repo.settings.ai_authorized(): return GatewayResult()
    bundle = repo.get("config_versions", run["config_bundle_id"])
    target_model = step.model_id or repo.settings.active_model_id
    bundle_body = (bundle or {}).get("body", {})
    try: profile = _leg_profile(repo, run["config_bundle_id"], "A5")
    except ValueError: return GatewayResult(error_code="CONFIG_GENERATION_MISMATCH")
    schema, input_cap, output_cap, instructions = LEGS["A5"]
    prompt = repo.get("config_versions", bundle["component_ids"].get("PROMPT_A5", "missing")) if bundle else None
    if (not bundle or profile.model_id != target_model or not prompt
        or prompt["body"] != {"leg_id": "A5", "instructions": instructions, "response_schema": schema.model_json_schema(),
            "input_cap": input_cap, "output_cap": output_cap}):
        return GatewayResult(error_code="CONFIG_MISMATCH")
    if repo.settings.mode == "cloud" and (bundle_body.get("provider") != "GOOGLE_CLOUD"
        or bundle_body.get("location") != repo.settings.verified_cloud_location): return GatewayResult(error_code="CONFIG_MISMATCH")
    step = step.model_copy(update={"prompt_version_id": prompt["id"], "selected_evidence_ids": context["evidence_ids"], "model_id": target_model})
    selected = evolution_prompt_context(context)
    text = instructions + "\nFROZEN_INPUTS:\n" + json.dumps(selected, sort_keys=True, default=str, separators=(",", ":"))
    # The prompt deliberately contains only a bounded sample.  Grounding still
    # validates against the entire frozen snapshot: aggregate pattern support
    # may legitimately name a feedback record outside that sample.
    return generate_json(repo, step, text, schema, input_limit=input_cap, output_limit=profile.max_output_tokens or output_cap, evidence={**context, "intelligence_version": 2},
                         model_id=target_model, thinking_level=profile.thinking_level, temperature=profile.temperature,
                         structured_output="A5" in bundle_body.get("structured_output_legs", []))


def _run_evolution(repo, run_id, lease=None):
    run = repo.get("pipeline_runs", run_id)
    _update(repo, run_id, {"state": "RUNNING", "stage": "A5"}, lease)
    profile = _leg_profile(repo, run["config_bundle_id"], "A5")
    step = StepRun(id=stable_id(run_id, "A5", 1), dataset_id=repo.dataset_id, run_id=run_id, leg_id="A5", state="STARTED",
        config_bundle_id=run["config_bundle_id"], started_at=now(), input_hash=run["analytical_snapshot_id"],
        model_id=profile.model_id, usage_status="UNKNOWN", cost_status="UNKNOWN",
        execution_provenance="UNKNOWN", worker_lease=lease)
    error = None
    checkpoint_id = step.id
    try:
        context = repo.evolution_context(run["analytical_snapshot_id"])
        sealed, error = _model_step(repo, step, lambda attempt: _evolution_leg(repo, run, attempt, context), context, lease)
        checkpoint_id = sealed["id"]
        if not error: repo.complete_evolution(run_id, lease=lease)
    except (Conflict, LookupError) as exc:
        if lease and str(exc) in {"WORKER_LEASE_LOST", "WORKER_EXECUTION_BOUND", "ATTEMPT_STILL_OWNED"}: raise
        error = "FROZEN_INPUT_INVALID"
    except Exception:
        if lease: raise
        error = "WORKER_FAILED"
    if error:
        if not _checkpoint(repo, step, lease):
            _create_step(repo, step.model_copy(update={"state": "FAILED", "finished_at": now(), "model_id": None,
                "usage_status": "NOT_APPLICABLE", "cost_status": "NOT_APPLICABLE",
                "execution_provenance": "NOT_SENT", "error_code": error}), lease)
        _update(repo, run_id, {"state": "FAILED", "completed_at": now(), "error_code": error,
            "checkpoint_step_id": checkpoint_id}, lease, finishing=True)


def run_evolution(repo, run_id):
    if not repo.claim_batch(run_id, "EVOLUTION"): return False
    _run_evolution(repo, run_id)
    return True


def run_delivery(repo, lease):
    run = repo.check_worker_lease(lease)
    if run["kind"] == "TRIAGE_BATCH": _run_batch(repo, lease.run_id, lease)
    elif run["kind"] == "EVOLUTION": _run_evolution(repo, lease.run_id, lease)
    else: raise Conflict("WORKER_RUN_INVALID")
    member_id = run.get("bulk_member")
    if member_id:
        from .bulk_jobs import advance
        member = repo.get("bulk_members", member_id)
        advance(Repository(repo.settings), member["job_id"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process bounded persisted work, then exit.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--delivery", action="store_true")
    args = parser.parse_args()
    from .config import Settings
    if args.watch:
        repo = Repository(Settings.load())
        if not repo.settings.local_gemini_valid(): raise SystemExit("Verified temporary local Free Tier access is required")
        from .models import DailyControlRequest
        try:
            while repo.settings.local_gemini_valid():
                worker_once(repo)
                sleep(2)
        finally:
            daily = repo.daily_control()
            repo.update_daily_control(DailyControlRequest(expected_revision=daily["revision"], live_enabled=False, limit=daily["limit"]), "local-worker")
    elif args.once:
        print(json.dumps({"processed_parent_runs": worker_once(Repository(Settings.load()))}))
    else:
        import sys
        from .models import WorkerLease
        try:
            lease = WorkerLease.model_validate_json(sys.stdin.read(10000))
            repo = Repository(Settings.load())
            from .bulk_jobs import scoped
            repo = scoped(repo, repo.get("pipeline_runs", lease.run_id))
            run_delivery(repo, lease)
            print(json.dumps({"state": repo.get("pipeline_runs", lease.run_id)["state"]}))
        except Exception:
            print(json.dumps({"state": "WORKER_INTERRUPTED"}))
            raise SystemExit(1)

