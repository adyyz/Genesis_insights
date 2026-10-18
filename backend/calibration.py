"""Deterministic, forward-validated A0 calibration. No provider or reference labels."""
from copy import deepcopy
from google.cloud import firestore
from .dataset import content_hash, stable_id
from .engine import RULES, PROTECTED
from .repository import Conflict, now

METHOD = "a0-cutoff-grid-v1"
GRID = (0, .10, .25, .40, .60, .65, .75, .90, 1)
BASELINE = "auto-label-v1"


def candidate_record(repo, ident, tx=None):
    snapshot = repo.db.collection("auto_label_candidates").document(ident).get(transaction=tx)
    row = {"id": snapshot.id, **snapshot.to_dict()} if snapshot.exists else None
    if not row or row.get("dataset_id") != repo.dataset_id: raise LookupError("A0 candidate not found")
    if row.get("training_chunks"):
        training = []
        for reference in row["training_chunks"]:
            chunk = repo.db.collection("auto_label_training_chunks").document(reference["id"]).get(transaction=tx)
            body = chunk.to_dict() if chunk.exists else None
            if not body or body.get("dataset_id") != repo.dataset_id or content_hash(body["rows"]) != reference["id"] or len(body["rows"]) != reference["count"]:
                raise Conflict("A0 training chunk integrity failed")
            training.extend(body["rows"])
        row["training"] = training
    return row


def candidate_summary(row):
    """History reads expose counts, not an unbounded training-reference array."""
    return {**row, "training_count": row.get("training_count", len(row.get("training", []))),
        "identity_count": row.get("identity_count", len({item["identity_id"] for item in row.get("training", [])})), "training": []}


def active_label_policy(repo, tx=None):
    doc = repo.db.collection("auto_label_controls").document(repo.dataset_id).get(transaction=tx)
    control = doc.to_dict() if doc.exists else {"policy_version_id": BASELINE, "revision": 0}
    if control["policy_version_id"] == BASELINE:
        rules = {rule: {"tp": .25, "fp": .65, "cohort": None} for rule in RULES}
    else:
        version = repo.db.collection("config_versions").document(control["policy_version_id"]).get(transaction=tx)
        row = version.to_dict() if version.exists else None
        if not row or row.get("kind") != "AUTO_LABEL_POLICY" or content_hash(row["body"]) != control["policy_version_id"]:
            raise Conflict("A0 policy integrity failed")
        if row["body"].get("dataset_id", repo.dataset_id) != repo.dataset_id:
            raise Conflict("A0 policy dataset mismatch")
        rules = row["body"]["rules"]
        if set(rules) != set(RULES) or any(not 0 <= value["tp"] < value["fp"] <= 1 for value in rules.values()):
            raise Conflict("Invalid A0 policy cutoffs")
    return {**control, "rules": rules}


def cohort(bundle):
    body = bundle.get("body", {})
    return content_hash({"formula": BASELINE, "detector": body.get("detector_version", "detector-v1"),
        "components": {key: bundle.get("component_ids", {}).get(key) for key in ("PROMPT_A2", "PROMPT_A3", "CONTEXT", "RISK_POLICY")},
        "models": {key: body.get("leg_models", {}).get(key, body.get("model_id")) for key in ("A2", "A3")}})


def classify(score, cutoffs):
    return "AUTO_FP" if score >= cutoffs["fp"] else "AUTO_TP" if score <= cutoffs["tp"] else "AUTO_UNCERTAIN"


def measured(rows, cutoffs, baseline):
    wrong_fp = wrong_tp = resolved_fp = resolved = uncertain = 0
    for row in rows:
        label = classify(row["score"], cutoffs)
        truth = "AUTO_FP" if row["verdict"] == "FALSE_POSITIVE" else "AUTO_TP"
        wrong_fp += label == "AUTO_FP" and truth == "AUTO_TP"
        wrong_tp += label == "AUTO_TP" and truth == "AUTO_FP"
        was_uncertain = classify(row["score"], baseline) == "AUTO_UNCERTAIN"
        resolved += was_uncertain and label == truth
        resolved_fp += was_uncertain and label == truth == "AUTO_FP"
        uncertain += label == "AUTO_UNCERTAIN"
    return {"wrong_fp": wrong_fp, "wrong_tp": wrong_tp, "correct_fp_resolutions": resolved_fp,
        "correct_resolutions": resolved, "uncertain": uncertain, "sample_size": len(rows)}


def safe_benefit(after, before):
    return after["wrong_fp"] <= before["wrong_fp"] and after["wrong_tp"] <= before["wrong_tp"] and after["correct_fp_resolutions"] > 0


def fit(rows, baseline, overall):
    identities = {verdict: {row["identity_id"] for row in rows if row["verdict"] == verdict}
        for verdict in ("TRUE_POSITIVE", "FALSE_POSITIVE")}
    if overall < 30 or len(rows) < 15 or any(len(ids) < 2 for ids in identities.values()): return None
    before = measured(rows, baseline, baseline)
    choices = []
    for tp in GRID:
        for fp in GRID:
            if tp >= fp: continue
            cutoffs = {"tp": tp, "fp": fp, "cohort": baseline.get("cohort")}
            score = measured(rows, cutoffs, baseline)
            if safe_benefit(score, before):
                choices.append(((-score["correct_fp_resolutions"], -score["correct_resolutions"],
                    round(abs(tp - baseline["tp"]) + abs(fp - baseline["fp"]), 8), -fp, tp), cutoffs, score))
    if not choices: return None
    _, cutoffs, score = min(choices, key=lambda item: item[0])
    return {"thresholds": cutoffs, "before": before, "after": score}


def observations(repo, feedback):
    output = []
    latest = {}
    for event in feedback:
        previous = latest.get(event.get("alert_id"))
        if not previous or event.get("revision", 0) > previous.get("revision", 0): latest[event.get("alert_id")] = event
    for event in latest.values():
        if event.get("provenance") != "HUMAN_OPERATOR" or event.get("verdict") not in {"TRUE_POSITIVE", "FALSE_POSITIVE"}: continue
        alert = repo.get("alerts", event["alert_id"])
        if not alert or alert.get("dataset_id") != repo.dataset_id: continue
        case = repo.operational_case(alert["case_id"])
        run = repo.get("pipeline_runs", event["triage_run_id"])
        if not run or run.get("dataset_id") != repo.dataset_id or run.get("state") != "COMPLETE" or run.get("alert_id") != alert["id"] or run.get("case_id") != alert["case_id"]: raise Conflict("Feedback run linkage failed")
        @firestore.transactional
        def read(tx): return repo._sealed_chain(tx, run)
        steps = read(repo.db.transaction(read_only=True))
        a0 = next((step["output"] for step in steps if step["leg_id"] == "A0"), {})
        if a0.get("requires_context_or_protected_review") is not False or a0.get("score_is_probability") is not False: continue
        if not isinstance(a0.get("heuristic_score"), (int, float)) or not 0 <= a0["heuristic_score"] <= 1 or alert["rule_id"] in PROTECTED: continue
        bundle = repo.get("config_versions", run["config_bundle_id"])
        original = repo.get("feedback_events", event["id"])
        if not original or original.get("dataset_id") != repo.dataset_id or original.get("case_id") != alert["case_id"]: raise Conflict("Supporting feedback disappeared")
        output.append({"feedback_id": event["id"], "alert_id": alert["id"], "identity_id": case["customer_id"],
            "rule_id": alert["rule_id"], "score": a0["heuristic_score"], "verdict": event["verdict"],
            "submitted_at": event["submitted_at"], "cohort": cohort(bundle), "feedback_hash": content_hash(original)})
    return output


def prepare(repo, snapshot_id, actor):
    context = repo.evolution_context(snapshot_id)  # Integrity and genuine-feedback checks.
    policy = active_label_policy(repo)
    current_cohort = cohort(repo.get("config_versions", repo.ensure_baseline()))
    rows = observations(repo, repo.calibration_feedback(snapshot_id) if hasattr(repo, "calibration_feedback") else context["human_feedback"])
    overall = context["analytical_inputs"]["sample_sizes"]["conclusive_reviews"]
    ident = stable_id(repo.dataset_id, METHOD, snapshot_id, policy["policy_version_id"], current_cohort)
    existing = repo.get("auto_label_calibrations", ident)
    if existing: return existing
    candidates, results = [], []
    for rule in RULES:
        selected = [row for row in rows if row["rule_id"] == rule and row["cohort"] == current_cohort]
        baseline = policy["rules"][rule]
        if baseline.get("cohort") not in {None, current_cohort}: baseline = {"tp": .25, "fp": .65, "cohort": None}
        fitted = fit(selected, baseline, overall) if rule not in PROTECTED else None
        diversity = {verdict: len({row["identity_id"] for row in selected if row["verdict"] == verdict}) for verdict in ("TRUE_POSITIVE", "FALSE_POSITIVE")}
        eligible = len(selected) >= 15 and overall >= 30 and all(count >= 2 for count in diversity.values())
        result = {"rule_id": rule, "sample_size": len(selected), "identity_count": len({row["identity_id"] for row in selected}), "verdict_identity_counts": diversity,
            "status": "PROTECTED_OVERRIDE" if rule in PROTECTED else "NO_BENEFICIAL_CANDIDATE" if eligible else "INSUFFICIENT_EVIDENCE"}
        if fitted:
            candidate = {"id": stable_id(ident, rule), "dataset_id": repo.dataset_id, "rule_id": rule,
                "calibration_id": ident, "analytical_snapshot_id": snapshot_id, "method": METHOD,
                "cohort": current_cohort, "baseline": baseline, **fitted,
                "training": selected, "training_hash": content_hash(selected), "frozen_at": now(),
                "state": "PROPOSED", "revision": 0, "validation": None}
            candidate["thresholds"]["cohort"] = current_cohort
            candidate["candidate_hash"] = content_hash({key: candidate[key] for key in ("rule_id", "thresholds", "training_hash", "cohort", "method", "baseline", "frozen_at")})
            if len(selected) > 200:
                from google.api_core.exceptions import AlreadyExists
                chunks = []
                for start in range(0, len(selected), 200):
                    rows = selected[start:start + 200]; chunk_id = content_hash(rows)
                    try: repo.create("auto_label_training_chunks", chunk_id, {"dataset_id": repo.dataset_id, "rows": rows})
                    except AlreadyExists:
                        if repo.get("auto_label_training_chunks", chunk_id).get("rows") != rows: raise Conflict("A0 training chunk changed")
                    chunks.append({"id": chunk_id, "count": len(rows)})
                candidate.update(training=[], training_chunks=chunks, training_count=len(selected), identity_count=len({row["identity_id"] for row in selected}))
            candidates.append(candidate)
            result.update(status="AWAITING_NEW_REVIEWS", candidate_id=candidate["id"])
        results.append(result)
    record = {"id": ident, "dataset_id": repo.dataset_id, "created_at": now(), "actor": actor,
        "analytical_snapshot_id": snapshot_id, "method": METHOD, "overall_reviews": overall, "results": results}
    @firestore.transactional
    def persist(tx):
        ref = repo.db.collection("auto_label_calibrations").document(ident)
        found = ref.get(transaction=tx)
        if found.exists: return {"id": found.id, **found.to_dict()}
        pending = []
        for item in candidates:
            lock = repo.db.collection("auto_label_pending").document(repo.dataset_id + '-' + item["rule_id"])
            prior = lock.get(transaction=tx)
            prior_row = repo.db.collection("auto_label_candidates").document(prior.to_dict()["candidate_id"]).get(transaction=tx) if prior.exists else None
            if prior_row and prior_row.exists and prior_row.to_dict()["state"] in {"PROPOSED", "APPROVED"}:
                raise Conflict("A candidate for this rule already awaits a decision")
            pending.append(lock)
        tx.create(ref, record)
        for item, lock in zip(candidates, pending):
            tx.create(repo.db.collection("auto_label_candidates").document(item["id"]), item)
            tx.set(lock, {"candidate_id": item["id"]})
        _audit(repo, tx, "A0_CANDIDATES_FROZEN", ident, actor)
        return record
    return repo._contended_transaction(persist)


def _audit(repo, tx, action, ident, actor, **values):
    from uuid import uuid4
    tx.create(repo.db.collection("audit_events").document(uuid4().hex), {"dataset_id": repo.dataset_id,
        "action": action, "object_collection": "auto_label_candidates", "object_id": ident,
        "actor": actor, "occurred_at": now(), **values})


def check_candidate(row):
    fields = ("rule_id", "thresholds", "training_hash", "cohort", "method", "baseline", "frozen_at")
    if row["rule_id"] in PROTECTED or row["method"] != METHOD or content_hash(row["training"]) != row["training_hash"] or content_hash({key: row[key] for key in fields}) != row["candidate_hash"]:
        raise Conflict("Candidate evidence integrity failed")
    thresholds = row["thresholds"]
    if not 0 <= thresholds["tp"] < thresholds["fp"] <= 1: raise Conflict("Invalid candidate thresholds")
    validation = row.get("validation")
    if validation:
        observations = validation["rows"]
        identities = {item["identity_id"] for item in observations}
        training_identities = {item["identity_id"] for item in row["training"]}
        before = measured(observations, row["baseline"], row["baseline"])
        after = measured(observations, thresholds, row["baseline"])
        passed = (len(observations) == len(identities) == 5 and not identities.intersection(training_identities)
            and all(item["submitted_at"] > row["frozen_at"] and item["cohort"] == row["cohort"] for item in observations)
            and len({item["verdict"] for item in observations}) == 2 and safe_benefit(after, before))
        if validation.get("provenance") != "PROSPECTIVE_HUMAN_REVIEWS" or content_hash(observations) != validation["content_hash"] or validation["before"] != before or validation["after"] != after or validation["passed"] != passed:
            raise Conflict("Prospective validation integrity failed")


def verify_references(repo, tx, observations):
    for observation in observations:
        event = repo.db.collection("feedback_events").document(observation["feedback_id"]).get(transaction=tx)
        value = {"id": event.id, **event.to_dict()} if event.exists else None
        if (not value or value.get("dataset_id") != repo.dataset_id or value.get("provenance") != "HUMAN_OPERATOR" or content_hash(value) != observation["feedback_hash"]
            or value.get("verdict") != observation["verdict"] or value.get("alert_id") != observation["alert_id"]):
            raise Conflict("Supporting genuine feedback changed")


def validate_candidate(repo, ident, actor):
    candidate = candidate_record(repo, ident)
    check_candidate(candidate)
    if candidate["validation"]: return candidate
    if candidate["state"] != "PROPOSED": raise Conflict("Only a proposed candidate can collect validation")
    if content_hash(candidate["training"]) != candidate["training_hash"]: raise Conflict("Training evidence changed")
    training_ids = {row["identity_id"] for row in candidate["training"]}
    feedback = [row for row in repo.list("feedback_events") if row["submitted_at"] > candidate["frozen_at"]]
    rows = sorted(observations(repo, feedback), key=lambda row: (row["submitted_at"], row["feedback_id"]))
    selected, seen = [], set(training_ids)
    for row in rows:
        if row["rule_id"] != candidate["rule_id"] or row["cohort"] != candidate["cohort"] or row["identity_id"] in seen: continue
        seen.add(row["identity_id"]); selected.append(row)
        if len(selected) == 5: break
    if len(selected) < 5: return {**candidate, "validation_progress": len(selected), "validation_required": 5}
    before = measured(selected, candidate["baseline"], candidate["baseline"])
    after = measured(selected, candidate["thresholds"], candidate["baseline"])
    passed = len({row["verdict"] for row in selected}) == 2 and safe_benefit(after, before)
    validation = {"rows": selected, "content_hash": content_hash(selected), "before": before, "after": after,
        "passed": passed, "provenance": "PROSPECTIVE_HUMAN_REVIEWS", "created_at": now()}
    @firestore.transactional
    def persist(tx):
        ref = repo.db.collection("auto_label_candidates").document(ident)
        row = ref.get(transaction=tx).to_dict()
        if row["validation"]: return {"id": ident, **row}
        if row["revision"] != candidate["revision"] or row["state"] != "PROPOSED": raise Conflict("Candidate changed")
        changes = {"validation": validation, "revision": row["revision"] + 1,
            "state": "PROPOSED" if passed else "VALIDATION_FAILED"}
        tx.update(ref, changes); _audit(repo, tx, "A0_VALIDATED", ident, actor, passed=passed)
        return {**row, **changes}
    return repo._contended_transaction(persist)


def decision(repo, ident, request, actor, action):
    @firestore.transactional
    def write(tx):
        ref = repo.db.collection("auto_label_candidates").document(ident)
        row = candidate_record(repo, ident, tx)
        check_candidate(row)
        if row["revision"] != request.expected_revision: raise Conflict("Candidate changed; refresh")
        policy = active_label_policy(repo, tx)
        changes = {"revision": row["revision"] + 1, "notes": request.notes, "decided_by": actor}
        if action == "review":
            if row["state"] != "PROPOSED": raise Conflict("Candidate decision is final")
            if request.state == "APPROVED" and not (row.get("validation") or {}).get("passed"):
                raise Conflict("Prospective validation has not established safe benefit")
            changes["state"] = request.state
        else:
            if policy["revision"] != request.expected_policy_revision: raise Conflict("A0 policy changed; refresh")
            if action == "rollback":
                if row["state"] != "APPLIED" or policy["policy_version_id"] != row.get("applied_policy_id"): raise Conflict("Only the current application can be rolled back")
                target = row["previous_policy_id"]
                if target != BASELINE:
                    previous = repo.db.collection("config_versions").document(target).get(transaction=tx)
                    if not previous.exists or content_hash(previous.to_dict()["body"]) != target: raise Conflict("Prior A0 policy integrity failed")
                changes.update(state="ROLLED_BACK", rolled_back_at=now())
            else:
                if row["state"] != "APPROVED" or not (row.get("validation") or {}).get("passed"): raise Conflict("Approved validated candidate required")
                verify_references(repo, tx, [*row["training"], *row["validation"]["rows"]])
                if policy["rules"][row["rule_id"]] != row["baseline"]: raise Conflict("Rule baseline changed")
                if content_hash(row["training"]) != row["training_hash"] or content_hash(row["validation"]["rows"]) != row["validation"]["content_hash"]:
                    raise Conflict("Calibration evidence integrity failed")
                rules = deepcopy(policy["rules"]); rules[row["rule_id"]] = row["thresholds"]
                body = {"method": METHOD, "rules": rules, "dataset_id": repo.dataset_id}; target = content_hash(body)
                version_ref = repo.db.collection("config_versions").document(target)
                version = version_ref.get(transaction=tx)
                if version.exists and version.to_dict().get("body") != body: raise Conflict("A0 version changed")
                if not version.exists: tx.create(version_ref, {"kind": "AUTO_LABEL_POLICY", "body": body, "content_hash": target, "created_at": now()})
                changes.update(state="APPLIED", applied_policy_id=target, previous_policy_id=policy["policy_version_id"], applied_at=now())
            tx.set(repo.db.collection("auto_label_controls").document(repo.dataset_id), {"policy_version_id": target, "revision": policy["revision"] + 1})
        tx.update(ref, changes); _audit(repo, tx, 'A0_' + action.upper(), ident, actor, notes=request.notes)
        return {**row, **changes}
    return repo._contended_transaction(write)


def reassess(repo, ident, request, actor):
    candidate = candidate_record(repo, ident)
    if not candidate or candidate.get("dataset_id") != repo.dataset_id or candidate["state"] not in {"APPLIED", "ROLLED_BACK"}: raise Conflict("Applied or rolled-back candidate required")
    check_candidate(candidate)
    if candidate["revision"] != request.expected_revision: raise Conflict("Candidate changed")
    policy = active_label_policy(repo)
    target = candidate["applied_policy_id"] if candidate["state"] == "APPLIED" else candidate["previous_policy_id"]
    if policy["policy_version_id"] != target or request.expected_policy_version != target: raise Conflict("A0 policy changed")
    allowed_cases = repo.operational_case_ids() if hasattr(repo, "operational_case_ids") else {row["id"] for row in repo.list("cases") if row.get("split") in {"DEMO", "CALIBRATION_DISCOVERY"}}
    alerts = sorted((row for row in repo.list("alerts") if row["case_id"] in allowed_cases and row["rule_id"] == candidate["rule_id"]
        and row["id"] > (request.cursor or "")), key=lambda row: row["id"])
    page = alerts[:50]; results = []
    for alert in page:
        prior_id = alert.get("latest_assessment_id")
        prior = repo.get("auto_label_assessments", prior_id) if prior_id else None
        if prior_id and (not prior or content_hash(prior["body"]) != prior["content_hash"] or prior["body"].get("source_run_id") != alert.get("current_run_id")):
            raise Conflict("Prior assessment integrity failed")
        run = repo.get("pipeline_runs", alert["current_run_id"]) if alert.get("current_run_id") else None
        if alert.get("feedback_revision", 0) or alert.get("status") != "COMPLETE" or not run or alert.get("latest_run_id") != run["id"]:
            results.append({"alert_id": alert["id"], "state": "SKIPPED", "reason": "Frozen, in progress or incomplete"}); continue
        repo.operational_case(alert["case_id"])
        @firestore.transactional
        def read(tx): return repo._sealed_chain(tx, run)
        steps = read(repo.db.transaction(read_only=True))
        a0 = next(step["output"] for step in steps if step["leg_id"] == "A0")
        current = prior["body"] if prior else a0
        restoring = candidate["state"] == "ROLLED_BACK"
        already = prior and prior["body"].get("candidate_id") == ident and prior["body"].get("label_policy_version") == target
        if already:
            results.append({"alert_id": alert["id"], "state": "SUCCEEDED", "assessment_id": prior_id}); continue
        eligible = current.get("predicted_verdict") == "AUTO_UNCERTAIN" if not restoring else prior and prior["body"].get("candidate_id") == ident
        bundle = repo.get("config_versions", run["config_bundle_id"])
        if not eligible or a0.get("requires_context_or_protected_review") is not False or cohort(bundle) != candidate["cohort"]:
            results.append({"alert_id": alert["id"], "state": "SKIPPED", "reason": "No compatible eligible assessment"}); continue
        thresholds = policy["rules"][candidate["rule_id"]]
        if thresholds.get("cohort") not in {None, candidate["cohort"]}: thresholds = {"tp": .25, "fp": .65}
        body = {"alert_id": alert["id"], "source_run_id": run["id"], "source_a0_hash": content_hash(a0),
            "candidate_id": ident, "label_policy_version": target, "heuristic_score": a0["heuristic_score"],
            "predicted_verdict": classify(a0["heuristic_score"], thresholds), "thresholds": thresholds,
            "provenance": "DETERMINISTIC_REASSESSMENT", "restoration": restoring}
        assessment_id = stable_id(ident, alert["id"], run["id"], target)
        @firestore.transactional
        def persist(tx):
            ref = repo.db.collection("alerts").document(alert["id"])
            fresh = ref.get(transaction=tx).to_dict()
            active = active_label_policy(repo, tx)
            assessment_ref = repo.db.collection("auto_label_assessments").document(assessment_id)
            existing = assessment_ref.get(transaction=tx)
            if active["policy_version_id"] != target: raise Conflict("A0 policy changed during reassessment")
            if fresh.get("feedback_revision", 0) or fresh.get("current_run_id") != run["id"] or fresh.get("latest_run_id") != run["id"] or fresh.get("status") != "COMPLETE" or fresh.get("latest_assessment_id") != prior_id:
                return {"alert_id": alert["id"], "state": "SKIPPED", "reason": "Alert changed concurrently"}
            if existing.exists and existing.to_dict()["content_hash"] != content_hash(body): raise Conflict("Assessment integrity failed")
            if not existing.exists: tx.create(assessment_ref, {"id": assessment_id, "dataset_id": repo.dataset_id, "body": body, "content_hash": content_hash(body), "created_at": now()})
            tx.update(ref, {"latest_assessment_id": assessment_id})
            _audit(repo, tx, "A0_RESTORED" if restoring else "A0_REASSESSED", assessment_id, actor, notes=request.notes)
            return {"alert_id": alert["id"], "state": "SUCCEEDED", "assessment_id": assessment_id}
        results.append(repo._contended_transaction(persist))
    return {"items": results, "next_cursor": page[-1]["id"] if len(alerts) > 50 else None,
        "policy_version_id": target, "model_requests": 0}
