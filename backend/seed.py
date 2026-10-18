"""Local emulator seed; never selects or creates a real cloud project."""
import firebase_admin
from firebase_admin import auth

from .config import Settings
from .dataset import DATASET_ID, generate_dataset
from .engine import detect, enrich, prioritize, risk_assessment
from .models import Split
from .repository import Repository, now


def public_cases(data, split=Split.DEMO):
    cases = []
    for case in data.cases.values():
        if case.split != split: continue
        evidence = enrich(data, case)
        risk = risk_assessment(evidence)
        hint = prioritize(evidence, risk, None)
        cases.append({"id": case.id, "name": data.customers[case.customer_id].display_name,
            "scenario_family": case.scenario_family, "split": split.value, "alerts": detect(data, case),
            "evidence": evidence, "risk": risk, "ai_status": "NOT_RUN",
            "queue_priority": hint["priority"], "queue_reason": hint["reason"]})
    return cases


def seed(settings: Settings | None = None):
    settings = settings or Settings.load()
    if settings.mode != "local": raise RuntimeError("Fixture seeding is restricted to local emulators")
    repo = Repository(settings)
    repo.ensure_baseline()
    data = generate_dataset()
    existing = repo.get("datasets", DATASET_ID)
    if existing and existing.get("state") == "READY":
        if existing["manifest_hash"] != data.manifest_hash: raise RuntimeError("Dataset is frozen; assign a new dataset version")
        print("Dataset already seeded; immutable source records unchanged.")
    else:
        repo.db.collection("datasets").document(DATASET_ID).set({"state": "SEEDING"})
        writes = []
        for name in ["customers", "accounts", "transactions", "cases"]:
            for ident, record in getattr(data, name).items():
                writes.append((name, ident, record.model_dump()))
        for case in data.cases.values():
            for alert in detect(data, case): writes.append(("alerts", alert["id"], alert))
        for offset in range(0, len(writes), 350):
            batch = repo.db.batch()
            for collection, ident, record in writes[offset:offset + 350]:
                batch.set(repo.db.collection(collection).document(ident), {"schema_version": 1, "created_at": now(), **record})
            batch.commit(timeout=30)
        repo.db.collection("datasets").document(DATASET_ID).set({"state": "READY", "schema_version": 1,
            "seed": data.seed, "version": data.version, "manifest_hash": data.manifest_hash,
            "reference_labels": data.reference_labels, "reference_reasons": data.reference_reasons,
            "split_manifest": {case.id: case.split.value for case in data.cases.values()}, "frozen_at": now()})
        repo.audit("SEEDED", "datasets", DATASET_ID, "local-seed")
    repo.publish(public_cases(data), {"dataset_id": DATASET_ID, "manifest_hash": data.manifest_hash,
        "case_count": 112, "discovery": 40, "selection": 20, "holdout": 40, "demo": 12,
        "note": "Fresh synthetic source facts and rule calculations. No AI results have been generated."}, "local-seed")
    app = firebase_admin.initialize_app(options={"projectId": settings.project_id}, name="local-seed")
    try:
        try: auth.get_user("operator-demo", app=app)
        except auth.UserNotFoundError:
            auth.create_user(uid="operator-demo", email="operator@aml-insight.test", password="local-demo-only", display_name="Local demo operator", app=app)
    finally:
        firebase_admin.delete_app(app)
    print("Ready: 112 synthetic cases; local operator operator@aml-insight.test / local-demo-only. Not a production credential.")


if __name__ == "__main__":
    seed()
