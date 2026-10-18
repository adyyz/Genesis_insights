"""Local-only controlled fixture. Its collections cannot reach corpus/operational policy."""
from types import SimpleNamespace
from google.cloud import firestore
from . import bulk_learning
from .calibration import classify, measured
from .dataset import content_hash
from .repository import Conflict, Repository, now

ID = "cutoff-exercise-v1"
PREFIX = "cutoff_exercise_"


class Fixture:
    manifest = {"dataset_id": ID, "manifest_hash": content_hash({"fixture": ID, "version": 1})}

    @staticmethod
    def observations(partition):
        counts = {"TRAIN": 40, "VALIDATION": 20, "TEST": 100}
        if partition not in counts: raise ValueError("Unknown fixture partition")
        # Deliberately separable scores prove plumbing, not real-world predictive quality.
        return [{"identity_id": f"EXERCISE_{partition}_{i}", "case_id": f"EXERCISE_{partition}_{i}",
            "rule_id": "HIGH_VALUE", "score": (0, .2, .5, .8)[i % 4],
            "verdict": "TRUE_POSITIVE" if i % 4 < 2 else "FALSE_POSITIVE",
            "provenance": "CONTROLLED_TEST_FIXTURE"} for i in range(counts[partition])]


def scoped(repo):
    if repo.settings.mode != "local": raise Conflict("The controlled exercise is available only with local emulators")
    worker = Repository(repo.settings)
    worker.db = SimpleNamespace(collection=lambda name: repo.db.collection(PREFIX + name), transaction=repo.db.transaction)
    return worker


def state(repo):
    worker = scoped(repo)
    records = list(worker.db.collection(bulk_learning.COLLECTION).limit(2).stream(timeout=10))
    if len(records) > 1: raise Conflict("Unexpected exercise records")
    return {"scope": ID, "provenance": "CONTROLLED_TEST_FIXTURE", "provider_calls": 0, "human_reviews": 0,
        "record": {"id": records[0].id, **records[0].to_dict()} if records else None,
        "policy": bulk_learning.policy(worker, ID)}


def act(repo, action, revision, actor):
    worker, fixture = scoped(repo), Fixture()
    if action == "prepare":
        if state(repo)["record"]: return state(repo)
        bulk_learning.prepare(worker, actor, data=fixture)
        return state(repo)
    current = state(repo)["record"]
    if not current: raise Conflict("Prepare the exercise candidate first")
    if action != "test":
        bulk_learning.act(worker, current["id"], action, revision, actor, data=fixture)
        return state(repo)
    ref = worker.db.collection(bulk_learning.COLLECTION).document(current["id"])

    @firestore.transactional
    def compare(tx):
        saved = ref.get(transaction=tx).to_dict()
        control = worker.db.collection("synthetic_controls").document(ID).get(transaction=tx)
        if saved["revision"] != revision: raise Conflict("Exercise changed; refresh before continuing")
        if saved["state"] != "APPLIED" or not control.exists or control.to_dict()["policy_id"] != saved.get("applied_policy_id"):
            raise Conflict("Apply the validated exercise cutoffs before testing")
        if saved.get("comparison"): return
        before = saved["body"]["baseline_policy"]["rules"]["HIGH_VALUE"]
        after = control.to_dict()["rules"]["HIGH_VALUE"]
        rows = fixture.observations("TEST")
        def counts(cutoffs):
            labels = [classify(r["score"], cutoffs) for r in rows]
            return {key: labels.count(key) for key in ("AUTO_TP", "AUTO_FP", "AUTO_UNCERTAIN")}
        result = {"cases": 100, "provenance": "CONTROLLED_TEST_FIXTURE", "provider_calls": 0,
            "before": counts(before), "after": counts(after), "baseline_cutoffs": before, "applied_cutoffs": after,
            "baseline_errors": measured(rows, before, before), "applied_errors": measured(rows, after, before),
            "test_hash": content_hash(rows), "policy_id": saved["applied_policy_id"]}
        tx.update(ref, {"comparison": result, "revision": revision + 1})
        tx.create(worker.db.collection("audit_events").document(content_hash(result)), {
            "action": "CONTROLLED_100_CASE_COMPARISON", "actor": actor, "occurred_at": now(), "dataset_id": ID})
    worker._contended_transaction(compare)
    return state(repo)
