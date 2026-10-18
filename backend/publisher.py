"""Offline source-publication plan by default; explicit reviewed cloud apply only."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from uuid import uuid4

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .config import Settings
from .dataset import DATASET_ID, content_hash, generate_dataset
from .engine import detect
from .repository import Conflict, Repository, now
from .seed import public_cases


PROJECT = "genesis-aicb"
MANIFEST = "9f297c1dd06ef395d9dad851348d15c5ffa4db95cedb49dc4ad7b5b36130dfa0"
SOURCE_COLLECTIONS = ("customers", "accounts", "transactions", "cases")
ALERT_MUTABLE = {"status", "feedback_revision", "current_run_id", "latest_run_id", "latest_parent_run_id", "priority"}
METADATA = {"created_at", "schema_version"}


def publication_plan(project, approved_manifest, database="(default)"):
    """Deterministic local facts only: no Settings, credentials or client creation."""
    if project != PROJECT or database != "(default)" or approved_manifest != MANIFEST:
        raise ValueError("Publication requires the approved project, default database and frozen manifest")
    data = generate_dataset()
    if data.manifest_hash != MANIFEST: raise Conflict("SOURCE_MANIFEST_CHANGED")
    cases = public_cases(data)
    return data, {"status": "OFFLINE_DRY_RUN", "project_id": project, "database": database,
        "dataset_id": DATASET_ID, "manifest_hash": data.manifest_hash,
        "source_counts": {name: len(getattr(data, name)) for name in SOURCE_COLLECTIONS},
        "public_case_count": len(cases), "public_cases_hash": content_hash(cases),
        "writes_performed": False, "cloud_connected": False}


def validate_approval(settings, approval):
    """Fail before ADC/client construction; this assertion is not proof of funding."""
    expiry = approval.get("credit_verification_expires_at")
    try: expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")) if isinstance(expiry, str) else None
    except ValueError: expiry = None
    if (settings.mode != "cloud" or settings.project_id != PROJECT or settings.firestore_host or settings.auth_host
        or settings.live_ai or settings.local_gemini_enabled or settings.cloud_tasks_enabled
        or not settings.credits_verified or settings.credit_verified_until is None
        or settings.credit_verified_until <= now() or expiry is None or expiry.tzinfo is None
        or expiry != settings.credit_verified_until):
        raise Conflict("PUBLICATION_FUNDING_OR_DISABLED_MODE_REQUIRED")
    identity = approval.get("publisher_identity", "")
    if (approval.get("status") != "APPROVED_FOR_SOURCE_PUBLICATION" or approval.get("source_publication_authorized") is not True
        or approval.get("project_id") != PROJECT or approval.get("database") != "(default)"
        or approval.get("manifest_hash") != MANIFEST
        or not isinstance(approval.get("approved_service_coverage"), list)
        or "firestore" not in approval["approved_service_coverage"]
        or not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@genesis-aicb\.iam\.gserviceaccount\.com", identity)
        or not settings.operator_uids or "operator-demo" in settings.operator_uids
        or not settings.admin_uids or not settings.admin_uids <= settings.operator_uids
        or sorted(settings.operator_uids) != approval.get("operator_uids")
        or sorted(settings.admin_uids) != approval.get("admin_uids")
        or not re.fullmatch(r"[0-9a-f]{64}", settings.deployed_code_fingerprint)
        or settings.deployed_code_fingerprint != approval.get("code_fingerprint")
        or not settings.verified_cloud_model_id or settings.verified_cloud_model_id != settings.model_id
        or not re.fullmatch(r"global|[a-z]+(?:-[a-z]+)+[0-9]+", settings.verified_cloud_location)):
        raise Conflict("PUBLICATION_REVIEW_REQUIRED")
    return identity


def _expected_records(data):
    records = {name: {ident: record.model_dump() for ident, record in getattr(data, name).items()}
        for name in SOURCE_COLLECTIONS}
    records["alerts"] = {alert["id"]: alert for case in data.cases.values() for alert in detect(data, case)}
    return records


def _check_records(repo, expected, *, complete=False):
    missing = []
    for collection, records in expected.items():
        existing = {row["id"]: row for row in repo.list(collection)}
        if existing.keys() - records.keys(): raise Conflict("UNEXPECTED_SOURCE_RECORD")
        mutable = ALERT_MUTABLE if collection == "alerts" else set()
        for ident, record in records.items():
            row = existing.get(ident)
            if row is None:
                if complete: raise Conflict("SOURCE_PREPARATION_INCOMPLETE")
                missing.append((collection, ident, record))
            elif ({key: value for key, value in row.items() if key not in METADATA | mutable}
                != {key: value for key, value in record.items() if key not in mutable}):
                raise Conflict("EXISTING_SOURCE_MISMATCH")
    return missing


def _manifest_content(data):
    return {"seed": data.seed, "version": data.version, "manifest_hash": data.manifest_hash,
        "reference_labels": data.reference_labels, "reference_reasons": data.reference_reasons,
        "split_manifest": {case.id: case.split.value for case in data.cases.values()}}


def _check_manifest(row, expected):
    if row and (row.get("state") not in {"SEEDING", "READY"}
        or {key: value for key, value in row.items() if key not in METADATA | {"id", "state", "frozen_at"}} != expected):
        raise Conflict("EXISTING_MANIFEST_MISMATCH")


def _check_bundle(repo, bundle_id):
    bundle = repo.get("config_versions", bundle_id)
    if not bundle: raise Conflict("CONFIGURATION_MISSING")
    for ident in [bundle_id, *bundle["component_ids"].values()]:
        record = repo.get("config_versions", ident)
        body = {key: value for key, value in (record or {}).items() if key not in METADATA | {"id", "content_hash"}}
        if not record or record.get("content_hash") != ident or content_hash(body) != ident:
            raise Conflict("EXISTING_CONFIGURATION_MISMATCH")


def _prepare_source(repo, data, actor):
    """Create-only persistence shared with demo-emulator checks; never copy exports."""
    if data.manifest_hash != MANIFEST: raise Conflict("SOURCE_MANIFEST_CHANGED")
    if not ((repo.settings.mode == "local" and repo.settings.project_id == "demo-aml-insight")
        or (repo.settings.mode == "cloud" and repo.settings.project_id == PROJECT)):
        raise Conflict("PUBLICATION_TARGET_INVALID")
    if repo.daily_control()["live_enabled"]:
        raise Conflict("PUBLICATION_DAILY_CONTROL_MUST_BE_DISABLED")
    manifest = _manifest_content(data)
    _check_manifest(repo.get("datasets", DATASET_ID), manifest)
    expected = _expected_records(data)
    missing = _check_records(repo, expected)
    existing = repo.get("datasets", DATASET_ID)
    if existing and existing["state"] == "READY" and missing:
        raise Conflict("READY_SOURCE_INCOMPLETE")
    if not existing:
        try: repo.create("datasets", DATASET_ID, {**manifest, "state": "SEEDING"})
        except AlreadyExists: _check_manifest(repo.get("datasets", DATASET_ID), manifest)
    for offset in range(0, len(missing), 350):
        batch = repo.db.batch()
        for collection, ident, record in missing[offset:offset + 350]:
            batch.create(repo.db.collection(collection).document(ident), {"schema_version": 1, "created_at": now(), **record})
        # Unknown/contended commits stop. An explicit rerun verifies and resumes.
        batch.commit(timeout=30)
    _check_records(repo, expected, complete=True)
    bundle_id = repo.ensure_baseline()
    _check_bundle(repo, bundle_id)

    @firestore.transactional
    def ready(tx):
        ref = repo.db.collection("datasets").document(DATASET_ID)
        snapshot = ref.get(transaction=tx)
        row = {"id": snapshot.id, **snapshot.to_dict()} if snapshot.exists else None
        if not row: raise Conflict("SOURCE_PREPARATION_INCOMPLETE")
        _check_manifest(row, manifest)
        if row["state"] == "READY": return
        tx.update(ref, {"state": "READY", "frozen_at": now()})
        tx.create(repo.db.collection("audit_events").document(uuid4().hex), {
            "dataset_id": DATASET_ID, "action": "SOURCE_PUBLICATION_PREPARED", "object_collection": "datasets",
            "object_id": DATASET_ID, "actor": actor, "occurred_at": now(), "after_hash": MANIFEST})
    ready(repo.db.transaction())
    return repo.publish(public_cases(data), {"dataset_id": DATASET_ID, "manifest_hash": MANIFEST,
        "case_count": 112, "discovery": 40, "selection": 20, "holdout": 40, "demo": 12,
        "note": "Fresh synthetic source facts and rule calculations. No AI results have been generated."}, actor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, choices=[PROJECT])
    parser.add_argument("--approved-manifest", required=True)
    parser.add_argument("--database", default="(default)", choices=["(default)"])
    parser.add_argument("--apply", action="store_true", help="Requires reviewed approval and disabled, funded cloud settings")
    parser.add_argument("--approval-file", type=Path, help="Private non-secret reviewed publication authorization JSON")
    args = parser.parse_args(argv)
    data, plan = publication_plan(args.project, args.approved_manifest, args.database)
    if args.apply:
        if not args.approval_file: raise Conflict("PUBLICATION_REVIEW_REQUIRED")
        approval = json.loads(args.approval_file.read_text(encoding="utf-8-sig"))
        if not isinstance(approval, dict): raise Conflict("PUBLICATION_REVIEW_REQUIRED")
        settings = Settings.load()
        identity = validate_approval(settings, approval)
        repo = Repository(settings)
        # Impersonated or attached service-account ADC only; no user-token fallback.
        if (repo.db.project != PROJECT or repo.db._database != "(default)"
            or getattr(repo.db._credentials, "service_account_email", None) != identity):
            raise Conflict("PUBLICATION_IDENTITY_MISMATCH")
        plan.update(status="SOURCE_PUBLISHED", cloud_connected=True, writes_performed=True,
            publication_id=_prepare_source(repo, data, identity))
    print(json.dumps(plan, sort_keys=True))
    return plan


if __name__ == "__main__":
    try: main()
    except Exception:
        # Provider/credential errors must not spill tokens, account details or paths.
        raise SystemExit("Source publication refused or interrupted; private details suppressed. Review configuration and persisted preparation before rerunning.") from None
