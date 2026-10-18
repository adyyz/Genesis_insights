"""Durable case-job coordinator; existing case parents own all provider attempts."""
from google.cloud import firestore
from google.api_core.exceptions import AlreadyExists
from .bulk_dataset import ID, corpus
from .dataset import content_hash, stable_id
from .engine import RULES, retains_high_value
from .repository import Conflict, Repository, now


class BulkRepository(Repository):
    def __init__(self, settings, case_id, dataset_id=ID, db=None):
        super().__init__(settings, ID)
        if db is not None: self.db = db
        self.dataset_id = dataset_id
        self.case_id = case_id
        if dataset_id == ID:
            if corpus().row(case_id)["partition"] != "PENDING": raise Conflict("Reference cohorts are not operational investigations")
        else:
            from .baseline_workspace import view, rows
            workspace = view(self)
            if not workspace or dataset_id != workspace["baseline_id"]: raise Conflict("Active baseline changed; refresh the queue")
            member = next((row for row in rows(self, workspace) if row["id"] == case_id), None)
            if not member or member["reference"]: raise Conflict("Only pending cases in the active baseline can be investigated")

    def load_dataset(self):
        if self.dataset_id != ID:
            from .baseline_workspace import source_dataset
            return source_dataset(self, self.case_id).model_copy(update={"id": self.dataset_id})
        return corpus().dataset(self.case_id).model_copy(update={"id": self.dataset_id})

    def source_case(self):
        from .engine import detect
        if self.dataset_id != ID:
            from .baseline_workspace import case
            source = case(self, self.case_id)
        else:
            source = corpus().case(self.case_id)
        data = self.load_dataset()
        return {**source, "dataset_id": self.dataset_id, "reference_record": None,
            "alerts": detect(data, data.cases[self.case_id])}

    def operational_case(self, case_id):
        if case_id != self.case_id: raise Conflict("Case scope mismatch")
        return self.load_dataset().cases[case_id].model_dump()

    def active_policy(self, tx=None):
        return super().active_policy(tx)

    def publication(self):
        return {"public_cases": []}

    def list(self, collection, dataset_id=None):
        if collection in {"alerts", "step_runs", "feedback_events"}:
            query = self.db.collection(collection).where(filter=firestore.FieldFilter("case_id", "==", self.case_id))
            return [{"id": doc.id, **doc.to_dict()} for doc in query.stream(timeout=10) if doc.to_dict().get("dataset_id") == self.dataset_id]
        return super().list(collection, dataset_id)

    def materialize(self):
        source = self.source_case()
        for alert in source["alerts"]:
            try: self.create("alerts", alert["id"], alert)
            except AlreadyExists:
                old = self.get("alerts", alert["id"])
                if not old or old["dataset_id"] != self.dataset_id or old["case_id"] != self.case_id or old["rule_id"] != alert["rule_id"]:
                    raise Conflict("Source alert integrity failed")
        return source

    def _bulk_progress(self, tx, case_id, changed_alert_id, changes):
        result = super()._bulk_progress(tx, case_id, changed_alert_id, changes)
        if result and self.dataset_id != ID:
            return self.db.collection("bulk_case_progress").document(stable_id(self.dataset_id, case_id)), result[1]
        return result


def scoped(repo, row):
    if not row: raise LookupError("Record not found")
    from .bulk_dataset import SEEDED_ID
    if row.get("dataset_id") == SEEDED_ID and row.get("case_id"):
        from .seeded_history import SeededRepository
        return SeededRepository(repo.settings, row["case_id"])
    if row.get("dataset_id") == ID:
        if row.get("case_id"): return BulkRepository(repo.settings, row["case_id"])
        if row.get("kind") == "EVOLUTION":
            from .bulk_analytics import AnalyticsRepository
            return AnalyticsRepository(repo.settings)
    from .baseline_workspace import selected_id, analytics
    baseline_id = selected_id(repo)
    if baseline_id and row.get("dataset_id") == baseline_id:
        if row.get("case_id"): return BulkRepository(repo.settings, row["case_id"], row["dataset_id"], repo.db)
        if row.get("kind") == "EVOLUTION": return analytics(repo)
    raise LookupError("Record not found in the current corpus")


def progress_source(row):
    safe = corpus().safe_row(row)
    return {**safe, "dataset_id": ID, "case_id": row["id"], "ordinal": row["ordinal"], "eligible": bool(safe["rules"]),
        "triaged": False, "reviewed": False, "triaged_alerts": 0, "reviewed_alerts": 0, "alerts": len(safe["rules"])}


def import_progress(repo):
    """Explicit local import, never an implicit cloud write or a status reset."""
    if repo.settings.mode != "local": raise Conflict("Bulk progress import requires reviewed cloud publication; this command is local-only")
    count, cursor = 0, 0
    while True:
        page = corpus().page(limit=100, cursor=cursor)
        refs = [repo.db.collection("bulk_case_progress").document(r["id"]) for r in page["items"]]
        existing = {doc.id for doc in repo.db.get_all(refs) if doc.exists}
        batch = repo.db.batch(); writes = 0
        for item, ref in zip(page["items"], refs):
            if item["id"] in existing: continue
            batch.create(ref, progress_source(corpus().row(item["id"]))); writes += 1
        if writes: batch.commit(timeout=30); count += writes
        cursor = page["next_cursor"]
        if cursor is None: break
    return count


def queue(repo, *, limit=50, cursor=0, identity="", customer_type="", rule="", query="", status="all"):
    from . import bulk_reference
    if status not in {"all", "reference", "awaiting_triage", "awaiting_review", "frozen"}: raise ValueError("Unknown triage status")
    if status == "reference": return bulk_reference.page(limit=limit, cursor=cursor, identity=identity, customer_type=customer_type, rule=rule, query=query)
    # The fixed fictional identity catalog supplies search terms; Firestore owns mutable state.
    with corpus().connect() as db:
        identities = [r[0] for r in db.execute("SELECT DISTINCT identity_id FROM cases WHERE partition='PENDING' AND name LIKE ?", ("%" + query[:100] + "%",))] if query else None
    if identity: identities = [identity] if identities is None or identity in identities else []
    if identities == []:
        empty = {"items": [], "total": 0, "next_cursor": None}
        return bulk_reference.merge(empty, limit=limit, cursor=cursor, identity=identity, customer_type=customer_type, rule=rule, query=query) if status == "all" else empty
    if rule and rule not in RULES: raise ValueError("Unknown detector rule")
    groups = [None] if identities is None else [identities[i:i + 10] for i in range(0, len(identities), 10)]
    rows, total = [], 0
    for group in groups:
        read = repo.db.collection("bulk_case_progress").where(filter=firestore.FieldFilter("dataset_id", "==", ID))
        if group: read = read.where(filter=firestore.FieldFilter("identity_id", "in", group))
        if customer_type: read = read.where(filter=firestore.FieldFilter("customer_type", "==", customer_type))
        if rule: read = read.where(filter=firestore.FieldFilter("rules", "array_contains", rule))
        if status == "awaiting_triage": read = read.where(filter=firestore.FieldFilter("eligible", "==", True)).where(filter=firestore.FieldFilter("triaged", "==", False))
        if status == "awaiting_review": read = read.where(filter=firestore.FieldFilter("triaged", "==", True)).where(filter=firestore.FieldFilter("reviewed", "==", False))
        if status == "frozen": read = read.where(filter=firestore.FieldFilter("reviewed", "==", True))
        total += read.count().get(timeout=10)[0][0].value
        for doc in read.where(filter=firestore.FieldFilter("ordinal", ">", cursor)).order_by("ordinal").limit(limit + 1).stream(timeout=10): rows.append({"id": doc.id, **doc.to_dict()})
    rows.sort(key=lambda r: r["ordinal"])
    result = {"items": rows[:limit], "total": total, "next_cursor": rows[limit - 1]["ordinal"] if len(rows) > limit else None,
        "manifest_hash": corpus().manifest["manifest_hash"]}
    return bulk_reference.merge(result, limit=limit, cursor=cursor, identity=identity, customer_type=customer_type, rule=rule, query=query) if status == "all" else result


def source(repo, case_id, dataset_id, *, materialize=True):
    worker = BulkRepository(repo.settings, case_id, dataset_id, repo.db)
    return worker, worker.materialize() if materialize else worker.source_case()


def eligible(repo, case):
    policy = repo.active_policy()
    existing = {row["id"]: row for row in repo.list("alerts") if row["case_id"] == case["id"]}
    rows = []
    for item in case["alerts"]:
        row = existing.get(item["id"], item)
        if row.get("feedback_revision", 0) or row["status"] in {"COMPLETE", "RUNNING", "QUEUED"}: continue
        if row["rule_id"] == "HIGH_VALUE" and not retains_high_value(case["evidence"], policy["candidate"]): continue
        rows.append(row["id"])
    return rows


def preview(repo, case_ids, dataset_id, actor):
    if not 1 <= len(case_ids) <= 100 or len(case_ids) != len(set(case_ids)): raise ValueError("Select 1-100 distinct cases")
    from .baseline_workspace import selected_id
    if dataset_id != (selected_id(repo) or ID): raise Conflict("Dataset changed; refresh the investigation queue")
    from .calibration import active_label_policy
    policy_repo, _ = source(repo, case_ids[0], dataset_id, materialize=False)
    policy = policy_repo.active_policy()
    labels = active_label_policy(policy_repo)
    members = []
    for ident in case_ids:
        worker, case = source(repo, ident, dataset_id, materialize=False)
        alerts = eligible(worker, case)
        members.append({"case_id": ident, "alert_ids": alerts, "protected": bool(case["evidence"]["protected_signals"]),
            "trigger_id": case["evidence"]["trigger"]["id"], "state": "PENDING" if alerts else "SKIPPED"})
    body = {"dataset_id": dataset_id, "members": members, "detector_policy": policy["policy_version_id"],
        "auto_label_policy": labels["policy_version_id"], "actor": actor}
    count = sum(len(row["alert_ids"]) for row in members)
    return {**body, "selection_hash": content_hash(body), "selected_cases": len(members), "eligible_cases": sum(bool(r["alert_ids"]) for r in members),
        "alerts": count, "minimum_generation_attempts": 3 * count, "maximum_generation_attempts": 6 * count,
        "known_estimated_usd": None, "unique_trigger_transactions": len({m["trigger_id"] for m in members}),
        "authorization_available": repo.settings.ai_authorized(), "daily": repo.daily_control()}


def create(repo, request, actor):
    ident = stable_id("bulk-job-v1", actor, request.idempotency_key)
    old = repo.get("bulk_jobs", ident)
    fingerprint = content_hash({"ids": request.case_ids, "dataset_id": request.dataset_id, "selection_hash": request.selection_hash})
    if old:
        if old["request_hash"] != fingerprint or old["actor"] != actor: raise Conflict("Idempotency key belongs to a different request")
        return kickoff(repo, detail(repo, ident))
    if repo.settings.mode == 'cloud':
        from .dispatch import dispatch_configured
        if not dispatch_configured(repo.settings): raise Conflict('CLOUD_DISPATCH_NOT_AUTHORIZED')
    plan = preview(repo, request.case_ids, request.dataset_id, actor)
    if plan["selection_hash"] != request.selection_hash: raise Conflict("Selection or policy changed; preview again")
    record = {"id": ident, "dataset_id": request.dataset_id, "actor": actor, "created_at": now(), "revision": 0,
        "state": "QUEUED", "request_hash": fingerprint, "selection_hash": plan["selection_hash"],
        "detector_policy": plan["detector_policy"], "auto_label_policy": plan["auto_label_policy"], "case_ids": request.case_ids}
    @firestore.transactional
    def save(tx):
        ref = repo.db.collection("bulk_jobs").document(ident); existing = ref.get(transaction=tx)
        if existing.exists:
            if existing.to_dict()["request_hash"] != fingerprint: raise Conflict("Idempotency conflict")
            return
        tx.create(ref, record)
        for member in plan["members"]:
            key = stable_id(ident, member["case_id"])
            tx.create(repo.db.collection("bulk_members").document(key), {**member, "id": key, "job_id": ident,
                "dataset_id": request.dataset_id, "parent_id": None})
        tx.create(repo.db.collection("audit_events").document(stable_id(ident, "created")), {
            "dataset_id": request.dataset_id, "action": "BULK_JOB_CREATED", "object_collection": "bulk_jobs", "object_id": ident, "actor": actor, "occurred_at": now()})
    repo._contended_transaction(save)
    return kickoff(repo, detail(repo, ident))


def kickoff(repo, job):
    if repo.settings.mode == 'cloud' and job['state'] == 'QUEUED':
        from .dispatch import dispatch_batch
        dispatch_batch(repo, job)
    return job


def detail(repo, ident):
    job = repo.get("bulk_jobs", ident)
    from .baseline_workspace import selected_id
    if not job or job.get("dataset_id") not in {ID, selected_id(repo)}: raise LookupError("Batch not found")
    members = [{"id": d.id, **d.to_dict()} for d in repo.db.collection("bulk_members").where(filter=firestore.FieldFilter("job_id", "==", ident)).limit(101).stream(timeout=10)]
    if len(members) != len(job["case_ids"]): raise Conflict("Batch membership integrity failed")
    for member in members:
        if member.get("parent_id"):
            parent = repo.get("pipeline_runs", member["parent_id"])
            if not parent or parent["case_id"] != member["case_id"] or parent["dataset_id"] != job["dataset_id"]: raise Conflict("Batch parent linkage failed")
            member["state"] = parent["state"]
            member["error_code"] = parent.get("error_code")
    counts = {state: sum(m["state"] == state for m in members) for state in ("PENDING", "QUEUED", "RUNNING", "COMPLETE", "FAILED", "SKIPPED", "DISPATCH_FAILED")}
    pending = next((m for m in members if m["state"] == "PENDING"), None)
    blockers = []
    dispatch_problem = (job.get('scheduler_revision') == job['revision'] and job.get('scheduler_outcome') in {'FAILED','UNKNOWN'})
    if job["state"] in {"QUEUED", "PAUSED", "PAUSED_CAPACITY", "PAUSED_OUTCOME"} and not counts["RUNNING"]:
        if not repo.settings.ai_authorized(): blockers.append("Verified local Gemini access is disabled or expired." if repo.settings.mode == "local" else "Verified cloud AI access is disabled or expired.")
        daily = repo.daily_control()
        if not daily["live_enabled"]: blockers.append("Today's AI switch is off in Admin.")
        required = 3 * len(pending["alert_ids"]) if pending else 0
        if daily["limit"] - daily["attempts_reserved"] < required: blockers.append("Today's remaining attempt allowance cannot cover the next case.")
    if job["state"] == "PAUSED_CAPACITY":
        message = " ".join(blockers) or "Access is ready. Resume this batch to continue."
    elif job["state"] == "QUEUED" and not counts["RUNNING"]:
        message = " ".join(blockers) or ("Batch scheduling failed or could not be confirmed. Resume explicitly to schedule a new revision." if dispatch_problem else "Queued for the separate worker; no case is running yet.")
    else:
        message = {"PAUSED": "This batch is paused. Resume it to continue.",
            "PAUSED_SELECTION": "The selected alerts or policy changed. Inspect the selection before starting a new batch.",
            "PAUSED_OUTCOME": "Inspect the failed case before acknowledging it and continuing the remaining cases."}.get(job["state"])
    return {**job, "members": members, "counts": counts, "waiting_message": message, "can_resume": not blockers and (job["state"] in {"PAUSED", "PAUSED_CAPACITY", "PAUSED_OUTCOME"} or job['state'] == 'QUEUED' and dispatch_problem) and repo.settings.ai_authorized()}


def decision(repo, ident, revision, action, actor, acknowledge_failed=False):
    observed = detail(repo, ident)
    if action == "resume" and not observed["can_resume"]: raise Conflict(observed["waiting_message"] or "This batch cannot be resumed")
    failed_parents = [m["parent_id"] for m in observed["members"] if m["state"] in {"FAILED", "DISPATCH_FAILED"} and m["parent_id"]]
    @firestore.transactional
    def change(tx):
        ref = repo.db.collection("bulk_jobs").document(ident); snap = ref.get(transaction=tx)
        current = snap.to_dict() if snap.exists else None
        if not current: raise LookupError("Batch not found")
        if current["revision"] != revision: raise Conflict("Batch changed; refresh")
        if current["state"] in {"COMPLETE", "FAILED", "CANCELLED"}: raise Conflict("Batch is terminal")
        if action not in {"pause", "resume", "cancel"}: raise ValueError("Unknown action")
        if action == "resume" and not repo.settings.ai_authorized(): raise Conflict("Verified live authorization required")
        if action == "resume" and current["state"] == "PAUSED_OUTCOME" and not acknowledge_failed:
            raise Conflict("Inspect and acknowledge failed members before continuing remaining cases")
        state = {"pause": "PAUSED", "resume": "QUEUED", "cancel": "CANCELLED"}[action]
        tx.update(ref, {"state": state, "revision": revision + 1,
            "acknowledged_parent_ids": sorted(set(current.get("acknowledged_parent_ids", [])) | (set(failed_parents) if acknowledge_failed else set()))})
        tx.create(repo.db.collection("audit_events").document(stable_id(ident, revision, action)), {
            "dataset_id": current["dataset_id"], "action": "BULK_" + action.upper(), "actor": actor,
            "object_collection": "bulk_jobs", "object_id": ident, "occurred_at": now()})
    repo._contended_transaction(change)
    job = detail(repo, ident)
    return kickoff(repo, job) if action == 'resume' else job


def set_job_state(repo, ident, revision, state):
    @firestore.transactional
    def save(tx):
        ref = repo.db.collection("bulk_jobs").document(ident); snap = ref.get(transaction=tx)
        row = snap.to_dict() if snap.exists else None
        if not row or row["revision"] != revision or row["state"] != "QUEUED": return False
        tx.update(ref, {"state": state, "revision": revision + 1})
        return True
    return repo._contended_transaction(save)


def advance(repo, ident):
    """Only the separate worker calls this. Schedule at most one case per turn."""
    job = detail(repo, ident)
    if job["state"] != "QUEUED": return job
    active = next((m for m in job["members"] if m["state"] in {"QUEUED", "RUNNING"}), None)
    if active:
        if repo.settings.mode == "cloud" and active["state"] == "QUEUED":
            from .dispatch import dispatch_run
            dispatch_run(scoped(repo, repo.get("pipeline_runs", active["parent_id"])), active["parent_id"])
        return job
    pending = next((m for m in job["members"] if m["state"] == "PENDING"), None)
    failed = any(m["state"] in {"FAILED", "DISPATCH_FAILED"} for m in job["members"])
    unacknowledged = any(m["state"] in {"FAILED", "DISPATCH_FAILED"} and m.get("parent_id") not in job.get("acknowledged_parent_ids", []) for m in job["members"])
    if pending is None:
        set_job_state(repo, ident, job["revision"], "FAILED" if failed else "COMPLETE")
        return detail(repo, ident)
    if unacknowledged:
        set_job_state(repo, ident, job["revision"], "PAUSED_OUTCOME")
        return detail(repo, ident)
    daily = repo.daily_control()
    if not repo.settings.ai_authorized() or not daily["live_enabled"] or daily["limit"] - daily["attempts_reserved"] < 3 * len(pending["alert_ids"]):
        set_job_state(repo, ident, job["revision"], "PAUSED_CAPACITY")
        return detail(repo, ident)
    worker, case = source(repo, pending["case_id"], job["dataset_id"])
    alerts = eligible(worker, case)
    if not alerts:
        repo.db.collection("bulk_members").document(pending["id"]).update({"state": "SKIPPED"})
        return detail(repo, ident)
    try:
        parent, created = worker.enqueue_triage(alerts, job["actor"], job["detector_policy"], job["auto_label_policy"], bulk_member=pending["id"])
    except (Conflict, ValueError):
        set_job_state(repo, ident, job["revision"], "PAUSED_SELECTION")
        return detail(repo, ident)
    if repo.settings.mode == "cloud":
        from .dispatch import dispatch_run
        dispatch_run(worker, parent)
    # Local work is claimed on the next worker pass; never in an API request.
    return detail(repo, ident)


if __name__ == "__main__":
    import argparse, json
    from .config import Settings
    parser = argparse.ArgumentParser(description="Explicit separate-worker batch scheduling tick")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job")
    group.add_argument("--import-local", action="store_true")
    args = parser.parse_args()
    repo = Repository(Settings.load())
    if args.import_local: print(json.dumps({"source_cases_initialized": import_progress(repo)}))
    else:
        result = advance(repo, args.job)
        print(json.dumps({"id": result["id"], "state": result["state"], "counts": result["counts"]}))
