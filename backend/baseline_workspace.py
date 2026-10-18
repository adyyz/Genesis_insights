"""Firestore serving index for the selected baseline; never an investigation or review."""
from collections import Counter

from google.api_core.exceptions import AlreadyExists

from .bulk_dataset import SEEDED_ID, corpus
from .dataset import content_hash
from .engine import RULES
from .repository import Conflict
from . import seeded_baseline


def selected_id(repo):
    pointer = repo.get("seeded_baseline_state", SEEDED_ID)
    return pointer["baseline_id"] if pointer else None


def view(repo):
    ident = selected_id(repo)
    if not ident: return None
    record = repo.get("baseline_workspace_views", ident)
    if not record: raise Conflict("Active baseline workspace index is missing; run the local baseline workspace import")
    baseline = repo.get("seeded_baselines", ident)
    if (not baseline or content_hash(baseline["body"]) != ident
        or content_hash(record["body"]) != record["content_hash"]
        or record["body"]["baseline_id"] != ident):
        raise Conflict("Active workspace integrity failed")
    return record["body"]


def prepare(repo, *, apply=False):
    if repo.settings.mode != "local": raise Conflict("Workspace preparation is emulator-only")
    selected = seeded_baseline.active(repo)
    if not selected: raise Conflict("Select a verified baseline first")
    from .seeded_history import catalog
    catalog_rows = catalog()
    rows = []
    # Validate every frozen history before the first write, including the reserved cohort.
    for scope in ("BASELINE", "RESERVED"):
        refs = selected[scope]
        for at in range(0, len(refs), 400):
            group = refs[at:at + 400]
            docs = {doc.id: doc.to_dict() for doc in repo.db.get_all([
                repo.db.collection("seeded_case_history").document(ref["history_id"]) for ref in group]) if doc.exists}
            for ref in group:
                history = docs.get(ref["history_id"])
                if (not history or content_hash(history["body"]) != ref["history_hash"]
                    or history["content_hash"] != ref["history_hash"]
                    or history["case_id"] != ref["case_id"]
                    or history["body"]["identity_id"] != ref["identity_id"]):
                    raise Conflict("Frozen baseline evidence changed")
                source = catalog_rows[ref["case_id"]]
                body = history["body"]
                row = {key: source[key] for key in ("id", "ordinal", "customer_type", "balance_rule")}
                row.update(identity_id=body["identity_id"], name=body["name"], rules=body["rules"],
                    heuristic_score=body["reference_heuristic_score"], requires_review=bool(body["protected_signals"] or body["missing_context"]),
                    risk_tier=body["python_risk"]["tier"], scope=scope, reference=scope == "BASELINE",
                    triaged=False, reviewed=False, history_id=ref["history_id"], history_hash=ref["history_hash"])
                if scope == "BASELINE":
                    row.update(reference_verdict=body["reference_verdict"], reference_prediction=body["reference_prediction"], reference_score=body["reference_heuristic_score"], reference_typology=source["reference_typology"])
                rows.append(row)
    rows.sort(key=lambda row: row["ordinal"])
    chunks = [{"rows": rows[at:at + 400]} for at in range(0, len(rows), 400)]
    body = {"baseline_id": selected["id"], "source_manifest_hash": selected["body"]["source_manifest_hash"],
        "chunks": [{"id": content_hash(chunk), "count": len(chunk["rows"])} for chunk in chunks],
        "cases": len(rows), "identities": len({row["identity_id"] for row in rows}),
        "baseline_cases": len(selected["BASELINE"]), "reserved_cases": len(selected["RESERVED"])}
    records = [("baseline_workspace_chunks", content_hash(chunk), {"body": chunk, "content_hash": content_hash(chunk)}) for chunk in chunks]
    records.append(("baseline_workspace_views", selected["id"], {"body": body, "content_hash": content_hash(body)}))
    for collection, ident, record in records:
        existing = repo.get(collection, ident)
        if existing and any(existing.get(key) != value for key, value in record.items()): raise Conflict("Workspace index changed")
    created = 0
    if apply:
        for collection, ident, record in records:
            try: repo.create(collection, ident, record); created += 1
            except AlreadyExists: pass
    return {**body, "apply": apply, "created": created, "model_calls": 0, "human_reviews_created": 0}


def rows(repo, workspace=None):
    workspace = workspace or view(repo)
    if not workspace: raise LookupError("Active baseline not found")
    output = []
    for reference in workspace["chunks"]:
        chunk = repo.get("baseline_workspace_chunks", reference["id"])
        if not chunk or content_hash(chunk["body"]) != reference["id"] or len(chunk["body"]["rows"]) != reference["count"]:
            raise Conflict("Workspace chunk changed")
        output.extend(chunk["body"]["rows"])
    if len(output) != workspace["cases"] or len({row["id"] for row in output}) != len(output): raise Conflict("Workspace membership changed")
    return output


def summary(repo, workspace):
    from .dispatch import dispatch_configured
    items = rows(repo, workspace)
    from .calibration import active_label_policy
    labels = active_label_policy(repo)
    progress = seeded_baseline.progress(repo)
    advisory = {key: progress[key] for key in ("id", "baseline_cases", "reserved_cases", "ai_reviewed_cases", "agent_draft_reviewed_cases", "pending_ai_reviews")}
    return {"dataset_id": workspace["baseline_id"], "scope": "ACTIVE_BASELINE", "manifest_hash": workspace["source_manifest_hash"],
        "cases": workspace["cases"], "transactions": workspace["cases"], "identities": workspace["identities"],
        "initially_unprocessed": workspace["reserved_cases"], "eligible_pending_cases": sum(bool(row["rules"]) for row in items if not row["reference"]),
        "reference_annotations": workspace["baseline_cases"], "training_transactions": workspace["baseline_cases"],
        "validation_transactions": workspace["reserved_cases"], "reference_walkthrough_cases": workspace["baseline_cases"],
        "rule_alerts": {rule: sum(rule in row["rules"] for row in items) for rule in RULES},
        "training_detector_metrics": None, "policy": {"policy_id": labels["policy_version_id"], "rules": labels["rules"]},
        "live_triage_available": repo.settings.mode == "local" or dispatch_configured(repo.settings), "advisory": advisory,
        "provider_calls": 0, "genuine_human_reviews": 0, "population_fpr": None, "reference_fp_share": None}


def queue(repo, *, limit=50, cursor=0, identity="", customer_type="", rule="", query="", status="all", private=False):
    workspace = view(repo)
    items = rows(repo, workspace)
    progress = {row["case_id"]: row for row in repo.list("bulk_case_progress", workspace["baseline_id"])} if private else {}
    queued = {}
    if private:
        from .bulk_jobs import detail
        for job in sorted(repo.list("bulk_jobs", workspace["baseline_id"]), key=lambda row: row["created_at"]):
            if job["state"] in {"COMPLETE", "FAILED", "CANCELLED"}: continue
            for member in detail(repo, job["id"])["members"]:
                state = job["state"] if job["state"].startswith("PAUSED") else member["state"]
                queued[member["case_id"]] = {"batch_id": job["id"], "batch_state": "QUEUED" if state == "PENDING" else state}
    items = [{**row, "batch_state": None, **queued.get(row["id"], {}), **{key: progress.get(row["id"], {}).get(key, False) for key in ("triaged", "reviewed")}} for row in items]
    items = [row for row in items if (not identity or identity == row["identity_id"])
        and (not customer_type or customer_type == row["customer_type"]) and (not rule or rule in row["rules"])
        and query.casefold() in row["name"].casefold()
        and (status == "all" or status == "reference" and row["reference"]
            or status == "awaiting_triage" and not row["reference"] and not row["triaged"]
            or status == "awaiting_review" and row["triaged"] and not row["reviewed"]
            or status == "frozen" and row["reviewed"])]
    after = [row for row in items if row["ordinal"] > cursor]
    return {"dataset_id": workspace["baseline_id"], "items": after[:limit], "total": len(items),
        "next_cursor": after[limit - 1]["ordinal"] if len(after) > limit else None}


def case(repo, ident):
    workspace = view(repo)
    row = next((row for row in rows(repo, workspace) if row["id"] == ident), None)
    if not row: raise LookupError("Case is outside the active baseline")
    # Full source evidence remains frozen; membership and displayed queue facts come from Firestore.
    if workspace.get('source_storage') == 'FIRESTORE':
        from .engine import detect, enrich, risk_assessment, prioritize
        data = source_dataset(repo, ident, workspace)
        selected = data.cases[ident]
        evidence = enrich(data, selected)
        risk = risk_assessment(evidence)
        source = {'id': ident, 'name': evidence['customer']['display_name'], 'split': 'CALIBRATION_DISCOVERY',
            'scenario_family': 'synthetic_bulk', 'alerts': detect(data, selected), 'evidence': evidence,
            'risk': risk, 'ai_status': 'NOT_RUN',
            **{'queue_' + key: value for key, value in prioritize(evidence, risk, None).items() if key in {'priority', 'reason'}}}
    else:
        source = corpus().case(ident)
    if source["evidence"]["customer"]["id"] != row["identity_id"]: raise Conflict("Source identity changed")
    return {**source, "dataset_id": workspace["baseline_id"], "reference_record": row if row["reference"] else None}


def source_dataset(repo, ident, workspace=None):
    """Read selected cloud evidence from Firestore; legacy local sources stay frozen."""
    workspace = workspace or view(repo)
    if workspace.get('source_storage') != 'FIRESTORE': return corpus().dataset(ident)
    from google.cloud import firestore
    from .models import Account, Case, Customer, Dataset, Transaction
    member = next((row for row in rows(repo, workspace) if row['id'] == ident), None)
    if not member: raise LookupError('Case is outside the active baseline')
    def model(collection, key, schema):
        record = repo.get(collection, key)
        if not record or record.get('dataset_id') != workspace['source_dataset_id']:
            raise Conflict('Cloud source document missing or outside scope')
        return schema.model_validate({key: value for key, value in record.items() if key in schema.model_fields})
    selected = model('cases', ident, Case)
    if selected.customer_id != member['identity_id']: raise Conflict('Cloud source identity mismatch')
    customer = model('customers', selected.customer_id, Customer)
    accounts = {key: model('accounts', key, Account) for key in selected.account_ids}
    if any(account.customer_id != customer.id for account in accounts.values()): raise Conflict('Cloud account linkage mismatch')
    expected_count = workspace['transactions_per_identity']
    query = repo.db.collection('transactions').where(filter=firestore.FieldFilter('customer_id', '==', customer.id)).limit(expected_count + 1)
    transactions = {}
    for doc in query.stream(timeout=15):
        record = doc.to_dict()
        if record.get('dataset_id') != workspace['source_dataset_id']: raise Conflict('Cloud history dataset mismatch')
        transaction = Transaction.model_validate({key: value for key, value in record.items() if key in Transaction.model_fields})
        if transaction.id != doc.id or transaction.account_id not in accounts: raise Conflict('Cloud transaction linkage mismatch')
        transactions[doc.id] = transaction
    if len(transactions) != expected_count or selected.trigger_transaction_id not in transactions:
        raise Conflict('Cloud transaction history incomplete')
    return Dataset(id=workspace['source_dataset_id'], seed=workspace['source_seed'], version=workspace['source_version'],
        customers={customer.id: customer}, accounts=accounts, transactions=transactions, cases={selected.id: selected},
        reference_labels={}, reference_reasons={}, manifest_hash=workspace['source_manifest_hash'])


def reference_summary(repo):
    workspace = view(repo)
    items = [row for row in rows(repo, workspace) if row["reference"]]
    def measured(group):
        counts = Counter(row["reference_verdict"] for row in group)
        comparable = [row for row in group if row["reference_verdict"] != "INCONCLUSIVE" and row["reference_prediction"] != "AUTO_UNCERTAIN"]
        return {"cases": len(group), "true_positive": counts["TRUE_POSITIVE"], "false_positive": counts["FALSE_POSITIVE"],
            "inconclusive": counts["INCONCLUSIVE"], "comparable": len(comparable),
            "agreement": sum((row["reference_prediction"] == "AUTO_TP") == (row["reference_verdict"] == "TRUE_POSITIVE") for row in comparable),
            "uncertain": sum(row["reference_prediction"] == "AUTO_UNCERTAIN" for row in group), "tp_cutoff": .25, "fp_cutoff": .65}
    return {"dataset_id": workspace["baseline_id"], "cases": len(items), "population_fraction": len(items) / workspace["cases"],
        "cohort_hash": content_hash(items), "metrics": measured(items), "identity_types": dict(Counter(row["customer_type"] for row in items)),
        "typologies": dict(Counter(row["reference_typology"] for row in items)),
        "rules": [{"rule_id": rule, **measured([row for row in items if rule in row["rules"]]), "balancing_cases": sum(row["balance_rule"] == rule for row in items)} for rule in RULES],
        "balance_note": "Active baseline only; reserved cases are excluded. Reference labels are not human reviews; rule counts overlap."}


def analytics(repo):
    from .bulk_analytics import AnalyticsRepository
    workspace = view(repo)
    result = AnalyticsRepository(repo.settings)
    result.db = repo.db
    if workspace:
        result.dataset_id = workspace["baseline_id"]
        result.workspace = workspace
    return result


class CurrentAnalytics:
    """Resolve scope at request time, including after a baseline pointer changes."""
    def __init__(self, repo): self.repo = repo
    def __getattr__(self, name): return getattr(analytics(self.repo), name)


def accounting(repo, operator_summary=False):
    from .bulk_analytics import pages, usage
    ident = selected_id(repo)
    if not ident: return usage(repo, operator_summary=operator_summary)
    def ledger():
        yield from pages(repo, "step_runs", dataset_id=ident)
        bundles = {}
        for group in pages(repo, "step_runs", dataset_id=SEEDED_ID):
            matched = []
            for step in group:
                bundle_id = step.get("config_bundle_id")
                if bundle_id not in bundles:
                    bundle = repo.get("config_versions", bundle_id) if bundle_id else None
                    bundles[bundle_id] = bool(bundle and content_hash(bundle["body"]) == bundle.get("content_hash") and bundle["body"].get("baseline_id") == ident)
                if bundles[bundle_id]: matched.append(step)
            if matched: yield matched
    return usage(repo, operator_summary=operator_summary, source_pages=ledger())


class ReferenceTraining:
    """Fit only the active reference cohort. Reserved cases stay unopened."""
    def __init__(self, repo):
        self.repo = repo
        self.workspace = view(repo)
        self.manifest = {"dataset_id": self.workspace["baseline_id"], "manifest_hash": self.workspace["source_manifest_hash"]}

    def observations(self, partition):
        if partition != "TRAIN": raise Conflict("Reserved cases remain unopened; this baseline has no approved independent validation cohort")
        return [{"identity_id": row["identity_id"], "case_id": row["id"], "rule_id": rule,
            "score": row["heuristic_score"], "verdict": row["reference_verdict"], "provenance": "SYNTHETIC_REFERENCE"}
            for row in rows(self.repo, self.workspace) if row["reference"] and not row["requires_review"]
            and row["reference_verdict"] in {"TRUE_POSITIVE", "FALSE_POSITIVE"} for rule in row["rules"]]
