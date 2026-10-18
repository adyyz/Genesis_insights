"""Synthetic cutoff demonstration, deliberately outside operational learning gates."""
from google.cloud import firestore
from .bulk_dataset import ID, corpus
from .calibration import fit, measured, safe_benefit
from .dataset import content_hash, stable_id
from .engine import RULES
from .repository import Conflict, now

COLLECTION = "synthetic_calibrations"


def policy(repo, dataset_id=ID):
    return repo.get("synthetic_controls", dataset_id) or {"revision": 0, "policy_id": "synthetic-baseline", "rules": {r: {"tp": .25, "fp": .65} for r in RULES}}


def prepare(repo, actor, *, data=None):
    data = data or corpus(); dataset_id = data.manifest["dataset_id"]
    rows = data.observations("TRAIN"); current = policy(repo, dataset_id)
    training_hash = content_hash(rows)
    ident = stable_id(dataset_id, "synthetic-cutoff-v1", training_hash, current["policy_id"])
    existing = repo.get(COLLECTION, ident)
    if existing: return existing
    if repo.get("synthetic_validation_controls", dataset_id):
        raise Conflict("This validation cohort has already been opened; independent learning requires a new untouched corpus")
    results = []
    for rule in RULES:
        selected = [row for row in rows if row["rule_id"] == rule]
        counts = {v: len({row["identity_id"] for row in selected if row["verdict"] == v}) for v in ("TRUE_POSITIVE", "FALSE_POSITIVE")}
        fitted = fit(selected, current["rules"][rule], len({r["case_id"] for r in rows}))
        results.append({"rule_id": rule, "sample_size": len(selected), "verdict_identities": counts,
            "generator_target_met": len(selected) >= 30 and min(counts.values()) >= 4,
            "status": "PROPOSED" if fitted else "INSUFFICIENT_EVIDENCE" if len(selected) < 15 or min(counts.values()) < 2 else "NO_BENEFICIAL_CANDIDATE",
            "candidate": fitted})
    body = {"dataset_id": dataset_id, "method": "synthetic-cutoff-v1", "manifest_hash": data.manifest["manifest_hash"],
        "training_hash": training_hash, "training_identities": sorted({r["identity_id"] for r in rows}),
        "baseline_policy": current, "results": results, "provenance": "SYNTHETIC_REFERENCE_NOT_HUMAN_REVIEW"}
    record = {"id": ident, "body": body, "candidate_hash": content_hash(body), "state": "PROPOSED", "revision": 0,
        "frozen_at": now(), "actor": actor, "validation": None}
    @firestore.transactional
    def save(tx):
        ref = repo.db.collection(COLLECTION).document(ident); previous = ref.get(transaction=tx)
        if previous.exists: return {"id": ident, **previous.to_dict()}
        tx.create(ref, record)
        return record
    return save(repo.db.transaction())


def act(repo, ident, action, revision, actor, *, data=None):
    ref = repo.db.collection(COLLECTION).document(ident)
    # Never inspect sealed validation until the frozen candidate has been verified.
    record = repo.get(COLLECTION, ident)
    if not record or content_hash(record["body"]) != record["candidate_hash"]: raise Conflict("Synthetic candidate integrity failed")
    dataset_id = record["body"]["dataset_id"]
    control = repo.db.collection("synthetic_controls").document(dataset_id)
    if record["revision"] != revision: raise Conflict("Synthetic candidate changed; refresh")
    validation = None
    if action == "validate":
        if record.get("validation") is not None: return record
        if not any(item["candidate"] for item in record["body"]["results"]):
            raise Conflict("No candidate to validate; baseline cutoffs are retained")
        @firestore.transactional
        def claim_validation(tx):
            lock = repo.db.collection("synthetic_validation_controls").document(dataset_id)
            frozen = ref.get(transaction=tx)
            opened = lock.get(transaction=tx)
            if not frozen.exists or frozen.to_dict()["candidate_hash"] != record["candidate_hash"] or frozen.to_dict()["revision"] != revision:
                raise Conflict("Frozen synthetic candidate changed")
            if opened.exists and opened.to_dict()["calibration_id"] != ident:
                raise Conflict("Validation was opened for another frozen candidate; use untouched data")
            if not opened.exists: tx.create(lock, {"calibration_id": ident, "candidate_hash": record["candidate_hash"], "opened_at": now()})
        repo._contended_transaction(claim_validation)
        validation_rows = (data or corpus()).observations("VALIDATION")
        if {r["identity_id"] for r in validation_rows}.intersection(record["body"]["training_identities"]): raise Conflict("Validation identities overlap training")
        results = []
        for item in record["body"]["results"]:
            if not item["candidate"]: continue
            rows = [r for r in validation_rows if r["rule_id"] == item["rule_id"]]
            baseline = record["body"]["baseline_policy"]["rules"][item["rule_id"]]
            before, after = measured(rows, baseline, baseline), measured(rows, item["candidate"]["thresholds"], baseline)
            classes = {r["verdict"] for r in rows}
            passed = classes == {"TRUE_POSITIVE", "FALSE_POSITIVE"} and safe_benefit(after, before)
            results.append({"rule_id": item["rule_id"], "before": before, "after": after, "passed": passed})
        validation = {"reference_hash": content_hash(validation_rows), "opened_at": now(), "results": results}
    @firestore.transactional
    def change(tx):
        snap = ref.get(transaction=tx); current = snap.to_dict() if snap.exists else None
        active_snap = control.get(transaction=tx); active = active_snap.to_dict() if active_snap.exists else policy(repo, dataset_id)
        if not current or current["revision"] != revision: raise Conflict("Synthetic candidate changed; refresh")
        if current["candidate_hash"] != record["candidate_hash"]: raise Conflict("Frozen candidate changed")
        changes = {"revision": revision + 1}
        if action == "validate":
            if current.get("validation") is not None: return current
            if current["state"] != "PROPOSED": raise Conflict("Validate before deciding")
            changes["validation"] = validation
        elif action in {"approve", "reject"}:
            if current["state"] != "PROPOSED": raise Conflict("Candidate already decided")
            if action == "approve" and not any(r["passed"] for r in (current.get("validation") or {}).get("results", [])):
                raise Conflict("No independently validated beneficial synthetic candidate")
            changes["state"] = "APPROVED" if action == "approve" else "REJECTED"
        elif action == "apply":
            if current["state"] != "APPROVED": raise Conflict("Approve before applying")
            if active["policy_id"] != current["body"]["baseline_policy"]["policy_id"]: raise Conflict("Synthetic policy changed")
            passed = {r["rule_id"] for r in current["validation"]["results"] if r["passed"]}
            rules = dict(active["rules"])
            for item in current["body"]["results"]:
                if item["rule_id"] in passed: rules[item["rule_id"]] = item["candidate"]["thresholds"]
            version = {"rules": rules, "candidate_hash": current["candidate_hash"], "provenance": "SYNTHETIC_SIMULATION_ONLY"}
            policy_id = content_hash(version)
            version_ref = repo.db.collection("synthetic_policies").document(policy_id)
            old = version_ref.get(transaction=tx)
            if not old.exists: tx.create(version_ref, version)
            tx.set(control, {"revision": active["revision"] + 1, "policy_id": policy_id, "rules": rules})
            changes.update(state="APPLIED", applied_policy_id=policy_id)
        else: raise ValueError("Unknown synthetic action")
        tx.update(ref, changes)
        tx.create(repo.db.collection("audit_events").document(stable_id(ident, revision, action)), {
            "dataset_id": dataset_id, "action": "SYNTHETIC_" + action.upper(), "actor": actor, "occurred_at": now(),
            "object_collection": COLLECTION, "object_id": ident})
        return {"id": ident, **current, **changes}
    return repo._contended_transaction(change)
