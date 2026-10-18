"""Explicit seeded history: reference reviews and real provider work never masquerade as humans."""
import argparse
import json
from collections import Counter
from functools import lru_cache

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .bulk_dataset import ID as SOURCE_ID, SEEDED_ID, corpus
from .bulk_reference import cohort
from .dataset import content_hash, stable_id
from .engine import detect
from .repository import Conflict, Repository, now

METHOD = "seeded-history-v1"
PROVENANCE = "SIMULATED_REFERENCE_REVIEW_NOT_HUMAN"


@lru_cache(maxsize=1)
def catalog():
    return {row["id"]: row for row in cohort()}


def history_record(row):
    source = corpus().case(row["id"])
    evidence = source["evidence"]
    body = {"dataset_id": SEEDED_ID, "source_dataset_id": SOURCE_ID, "case_id": row["id"],
        "manifest_hash": corpus().manifest["manifest_hash"], "method": METHOD, "provenance": PROVENANCE,
        "identity_id": row["identity_id"], "name": row["name"], "rules": row["rules"],
        "reference_verdict": row["reference_verdict"], "reference_prediction": row["reference_prediction"],
        "reference_heuristic_score": row["reference_score"], "human_review": False,
        "ai_investigation": "NOT_RUN", "risk_weights_changed": False,
        "python_risk": source["risk"], "evidence_facts": evidence["facts"],
        "signals": evidence["signals"], "protected_signals": evidence["protected_signals"],
        "missing_context": evidence["missing_context"], "deterministic_provenance": "PYTHON_COMPUTED"}
    return {"id": stable_id(SEEDED_ID, row["id"]), "dataset_id": SEEDED_ID,
        "case_id": row["id"], "body": body, "content_hash": content_hash(body)}


def prepare(repo, actor):
    if repo.settings.mode != "local": raise Conflict("Seeded history preparation is local-only")
    rows = catalog()
    existing = {row["case_id"]: row for row in repo.list("seeded_case_history", SEEDED_ID)}
    created = 0
    batch = repo.db.batch(); writes = 0
    for row in rows.values():
        record = history_record(row)
        if row["id"] in existing:
            old = existing[row["id"]]
            if old.get("content_hash") != record["content_hash"] or content_hash(old["body"]) != old["content_hash"]: raise Conflict("Seeded reference history integrity failed")
            continue
        batch.create(repo.db.collection("seeded_case_history").document(record["id"]), {**record, "created_at": now(), "actor": actor})
        writes += 1; created += 1
        if writes == 400:
            batch.commit(timeout=30); batch = repo.db.batch(); writes = 0
    if writes: batch.commit(timeout=30)
    # Fit only training observations from the selected reference cohort. No held-out read.
    from .bulk_learning import prepare as learn
    class Training:
        manifest = {**corpus().manifest, "dataset_id": SEEDED_ID}
        def observations(self, partition):
            if partition != "TRAIN": raise Conflict("Seeded preparation cannot open validation")
            return [row for row in corpus().observations("TRAIN") if row["case_id"] in rows]
    learning = learn(repo, actor, data=Training())
    repo.audit("SEEDED_HISTORY_PREPARED", "seeded_case_history", SEEDED_ID, actor,
        synthetic_reviews=len(rows), genuine_human_reviews=0, created=created, learning_id=learning["id"])
    return {"cases": len(rows), "created": created, "learning_id": learning["id"],
        "provenance": PROVENANCE, "results": learning["body"]["results"], "model_calls": 0}


class SeededRepository(Repository):
    def __init__(self, settings, case_id):
        if case_id not in catalog(): raise LookupError("Case is outside the seeded training cohort")
        super().__init__(settings, SEEDED_ID)
        self.case_id = case_id

    def load_dataset(self):
        return corpus().dataset(self.case_id).model_copy(update={"id": SEEDED_ID})

    def operational_case(self, case_id):
        if case_id != self.case_id: raise Conflict("Seeded case scope mismatch")
        return self.load_dataset().cases[case_id].model_dump()

    def publication(self): return {"public_cases": []}

    def list(self, collection, dataset_id=None):
        if collection in {"alerts", "step_runs", "feedback_events"}:
            query = self.db.collection(collection).where(filter=firestore.FieldFilter("case_id", "==", self.case_id))
            return [{"id": d.id, **d.to_dict()} for d in query.stream(timeout=15) if d.to_dict().get("dataset_id") == SEEDED_ID]
        return super().list(collection, dataset_id)

    def materialize(self):
        if not self.get("seeded_case_history", stable_id(SEEDED_ID, self.case_id)):
            raise Conflict("Prepare the reference history before a Gemini investigation")
        data = self.load_dataset(); source = data.cases[self.case_id]
        alerts = detect(data, source)
        for alert in alerts:
            try: self.create("alerts", alert["id"], alert)
            except AlreadyExists:
                old = self.get("alerts", alert["id"])
                if any(old.get(key) != alert[key] for key in ["dataset_id", "case_id", "rule_id"]): raise Conflict("Seeded alert integrity failed")
        return alerts

    def save_feedback(self, *_args, **_kwargs):
        raise Conflict("Seeded reference reviews are simulated; genuine analyst feedback belongs to operational investigations")

    def _bulk_progress(self, tx, case_id, changed_alert_id, changes):
        query = self.db.collection("alerts").where(filter=firestore.FieldFilter("case_id", "==", case_id))
        rows = [{"id": d.id, **d.to_dict()} for d in query.stream(transaction=tx) if d.to_dict().get("dataset_id") == SEEDED_ID]
        for row in rows:
            if row["id"] == changed_alert_id: row.update(changes)
        return self.db.collection("seeded_ai_progress").document(case_id), {
            "dataset_id": SEEDED_ID, "case_id": case_id, "alerts": len(rows),
            "completed_alerts": sum(row.get("status") == "COMPLETE" for row in rows),
            "complete": bool(rows) and all(row.get("status") == "COMPLETE" for row in rows), "updated_at": now()}


def summary(repo):
    history = repo.db.collection("seeded_case_history").where(filter=firestore.FieldFilter("dataset_id", "==", SEEDED_ID)).count().get(timeout=15)[0][0].value
    parents = repo.page("pipeline_runs", 10, filters=[("dataset_id", SEEDED_ID), ("kind", "TRIAGE_BATCH")], order="created_at")["items"]
    run_states = {}
    query = repo.db.collection("pipeline_runs").where(filter=firestore.FieldFilter("dataset_id", "==", SEEDED_ID)).where(filter=firestore.FieldFilter("kind", "==", "TRIAGE_BATCH"))
    for state in ["COMPLETE", "FAILED", "QUEUED", "RUNNING", "DISPATCH_FAILED"]:
        count = query.where(filter=firestore.FieldFilter("state", "==", state)).count().get(timeout=15)[0][0].value
        if count: run_states[state] = count
    complete = repo.db.collection("seeded_ai_progress").where(filter=firestore.FieldFilter("dataset_id", "==", SEEDED_ID)).where(filter=firestore.FieldFilter("complete", "==", True)).count().get(timeout=15)[0][0].value
    children = [child for run in parents for child in run["child_run_ids"]]
    steps = []
    for at in range(0, len(children), 30):
        query = repo.db.collection("step_runs").where(filter=firestore.FieldFilter("dataset_id", "==", SEEDED_ID)).where(filter=firestore.FieldFilter("run_id", "in", children[at:at + 30]))
        steps.extend({"id": d.id, **d.to_dict()} for d in query.limit(240).stream(timeout=15))
    from .bulk_analytics import usage
    from .bulk_learning import policy
    comparisons = list(repo.db.collection("seeded_model_evaluations").where(filter=firestore.FieldFilter("dataset_id", "==", SEEDED_ID)).order_by("created_at", direction=firestore.Query.DESCENDING).limit(1).stream(timeout=15))
    learning = repo.page("synthetic_calibrations", 10, filters=[("body.dataset_id", SEEDED_ID)], order="frozen_at")["items"]
    examples = []
    for run in sorted(parents, key=lambda r: r.get("created_at"), reverse=True)[:10]:
        # Steps link to alert children; resolve through the parent's recorded children.
        outputs = [s for s in steps if s["run_id"] in run["child_run_ids"] and s.get("state") == "SUCCEEDED"]
        briefs = [s["output"] for s in outputs if s["leg_id"] == "A4"]
        labels = [s["output"]["predicted_verdict"] for s in outputs if s["leg_id"] == "A0"]
        reference = catalog()[run["case_id"]]
        examples.append({"run_id": run["id"], "case_id": run["case_id"], "name": reference["name"],
            "state": run["state"], "error_code": run.get("error_code"), "machine_labels": labels,
            "simulated_verdict": reference["reference_verdict"], "briefs": briefs})
    from .seeded_baseline import progress
    return {"scope": SEEDED_ID, "provenance": PROVENANCE, "reference_cases": history, "baseline": progress(repo),
        "genuine_human_reviews": 0, "completed_ai_cases": complete, "pending_ai_cases": history - complete,
        "run_states": run_states, "usage": usage(repo, dataset_id=SEEDED_ID),
        "policy": policy(repo, SEEDED_ID), "learning": [{"id": r["id"], "state": r["state"], "results": r["body"]["results"]} for r in learning],
        "model_comparison": {key: comparisons[0].to_dict()["body"][key] for key in ["counts", "results", "note"]} if comparisons else None,
        "examples": examples, "risk_weights_changed": False, "operational_policy_changed": False}


def comparison_counts(observations):
    comparable = [r for r in observations if r["predicted_verdict"] in {"AUTO_TP", "AUTO_FP"} and r["reference_verdict"] in {"TRUE_POSITIVE", "FALSE_POSITIVE"}]
    return {"alerts": len(observations), "cases": len({r["case_id"] for r in observations}),
        "identities": len({r["identity_id"] for r in observations}), "comparable": len(comparable),
        "agreed": sum((r["predicted_verdict"] == "AUTO_TP") == (r["reference_verdict"] == "TRUE_POSITIVE") for r in comparable),
        "uncertain": sum(r["predicted_verdict"] == "AUTO_UNCERTAIN" for r in observations),
        "reference_inconclusive": sum(r["reference_verdict"] == "INCONCLUSIVE" for r in observations)}


def evaluate(repo, actor):
    """Freeze measured model/reference comparisons without inventing human feedback."""
    from .calibration import fit
    from .engine import RULES
    alerts = {r["id"]: r for r in repo.list("alerts", SEEDED_ID)}
    runs = {r["id"]: r for r in repo.list("pipeline_runs", SEEDED_ID)}
    observations = []
    for step in repo.list("step_runs", SEEDED_ID):
        if step["leg_id"] != "A0" or step["state"] != "SUCCEEDED": continue
        run = runs[step["run_id"]]; alert = alerts[run["alert_id"]]
        if run["state"] != "COMPLETE" or alert.get("current_run_id") != run["id"]: continue
        reference = catalog()[run["case_id"]]; output = step["output"]
        observations.append({"step_id": step["id"], "case_id": run["case_id"], "rule_id": alert["rule_id"],
            "identity_id": reference["identity_id"], "reference_verdict": reference["reference_verdict"],
            "predicted_verdict": output["predicted_verdict"], "score": output["heuristic_score"],
            "protected_or_missing": output["requires_context_or_protected_review"],
            "policy_version": output["label_policy_version"], "cohort": output.get("cohort")})
    observations.sort(key=lambda row: row["step_id"])
    eligible = [r for r in observations if not r["protected_or_missing"] and r["policy_version"] == "auto-label-v1"
        and r["reference_verdict"] in {"TRUE_POSITIVE", "FALSE_POSITIVE"}]
    results = []
    for rule in RULES:
        selected = [{**r, "verdict": r["reference_verdict"]} for r in eligible if r["rule_id"] == rule]
        identities = {v: len({r["identity_id"] for r in selected if r["verdict"] == v}) for v in ["TRUE_POSITIVE", "FALSE_POSITIVE"]}
        candidate = fit(selected, {"tp": .25, "fp": .65}, len(eligible))
        insufficient = len(eligible) < 30 or len(selected) < 15 or min(identities.values()) < 2
        incompatible = len({content_hash(r["cohort"]) for r in selected}) > 1
        results.append({"rule_id": rule, "sample_size": len(selected), "verdict_identities": identities,
            "status": "INCOMPATIBLE_VERSIONS" if incompatible else "INSUFFICIENT_EVIDENCE" if insufficient else "PROPOSED" if candidate else "NO_BENEFICIAL_CANDIDATE",
            "candidate": candidate if not insufficient and not incompatible else None})
    chunks = []
    for start in range(0, len(observations), 400):
        rows = observations[start:start + 400]; chunk_id = content_hash(rows)
        old = repo.get("seeded_model_observation_chunks", chunk_id)
        if old:
            if content_hash(old["rows"]) != chunk_id: raise Conflict("Seeded observation integrity failed")
        else:
            try: repo.create("seeded_model_observation_chunks", chunk_id, {"dataset_id": SEEDED_ID, "rows": rows})
            except AlreadyExists:
                if content_hash(repo.get("seeded_model_observation_chunks", chunk_id)["rows"]) != chunk_id: raise Conflict("Seeded observation integrity failed")
        chunks.append({"id": chunk_id, "count": len(rows)})
    body = {"dataset_id": SEEDED_ID, "method": "seeded-model-comparison-v2", "provenance": PROVENANCE,
        "observation_chunks": chunks, "counts": comparison_counts(observations), "results": results,
        "note": "Real Gemini machine labels compared with simulated reference verdicts; not human agreement or population accuracy."}
    ident = content_hash(body)
    previous = repo.get("seeded_model_evaluations", ident)
    if previous: return previous
    record = {"id": ident, "dataset_id": SEEDED_ID, "body": body, "content_hash": ident, "created_at": now()}
    try: repo.create("seeded_model_evaluations", ident, record)
    except AlreadyExists: return repo.get("seeded_model_evaluations", ident)
    repo.audit("SEEDED_MODEL_COMPARISON_FROZEN", "seeded_model_evaluations", ident, actor, genuine_human_reviews=0)
    return record


def run_pending(repo, actor, max_cases=0):
    if repo.settings.mode != "local" or not repo.settings.local_gemini_valid(): raise Conflict("Verified local Gemini Free Tier access is required")
    from .calibration import active_label_policy
    from .jobs import run_local
    from .models import DailyControlRequest
    current = repo.daily_control()
    repo.update_daily_control(DailyControlRequest(expected_revision=current["revision"], live_enabled=True, limit=120), actor)
    processed = 0
    try:
        history = {r["case_id"] for r in repo.list("seeded_case_history", SEEDED_ID)}
        previous = repo.list("pipeline_runs", SEEDED_ID)
        attempted = {r["case_id"] for r in previous if r["kind"] == "TRIAGE_BATCH"}
        from .seeded_baseline import active
        baseline = active(repo)
        allowed = {r['case_id'] for r in baseline['BASELINE']} if baseline else set(catalog())
        for row in catalog().values():
            if row["id"] not in allowed or row["id"] not in history or row["id"] in attempted: continue
            if not repo.settings.local_gemini_valid(): return {"processed_cases": processed, "stop": "VERIFICATION_EXPIRED"}
            daily = repo.daily_control()
            if not daily["live_enabled"] or daily["limit"] - daily["attempts_reserved"] < 3 * len(row["rules"]):
                return {"processed_cases": processed, "stop": "DAILY_CAP"}
            worker = SeededRepository(repo.settings, row["id"]); worker.db = repo.db
            alerts = worker.materialize()
            parent, _ = worker.enqueue_triage([a["id"] for a in alerts], actor,
                worker.active_policy()["policy_version_id"], active_label_policy(worker)["policy_version_id"])
            run_local(worker, parent)
            run = worker.get("pipeline_runs", parent)
            processed += 1
            print(json.dumps({"processed_cases": processed, "run_id": parent, "state": run["state"], "error_code": run.get("error_code")}), flush=True)
            if run["state"] != "COMPLETE": return {"processed_cases": processed, "stop": run.get("error_code") or "FAILED_CASE"}
            if max_cases and processed >= max_cases: return {"processed_cases": processed, "stop": "REQUESTED_CASE_LIMIT"}
        return {"processed_cases": processed, "stop": "COMPLETE"}
    finally:
        daily = repo.daily_control()
        repo.update_daily_control(DailyControlRequest(expected_revision=daily["revision"], live_enabled=False, limit=daily["limit"]), actor)
        evaluate(repo, actor)


if __name__ == "__main__":
    from .config import Settings
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "run", "evaluate", "status"])
    parser.add_argument("--max-cases", type=int, choices=range(0, 10001), default=0)
    args = parser.parse_args(); repo = Repository(Settings.load(), SEEDED_ID)
    result = prepare(repo, "seeded-history-import") if args.action == "prepare" else run_pending(repo, "operator-demo", args.max_cases) if args.action == "run" else evaluate(repo, "operator-demo") if args.action == "evaluate" else summary(repo)
    print(json.dumps(result, default=str), flush=True)
