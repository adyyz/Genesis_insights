"""Deterministic simulated analyst reactions; never human feedback or provider output."""
from collections import Counter, defaultdict

from google.api_core.exceptions import AlreadyExists

from . import baseline_workspace as workspace
from .bulk_dataset import SEEDED_ID, corpus
from .calibration import fit
from .dataset import content_hash, stable_id
from .engine import RULES
from .repository import Conflict

METHOD = "projected-analyst-75-20-5-v1"
PROVENANCE = "SIMULATED_ANALYST_PROJECTION_NOT_HUMAN"
MODES = {"AGREE": 75, "DISAGREE": 20, "UNCERTAIN": 5}
VERDICTS = {"TRUE_POSITIVE", "FALSE_POSITIVE", "INCONCLUSIVE"}


def assignments(rows, baseline_id):
    if len(rows) != 2000 or len({row["id"] for row in rows}) != len(rows): raise ValueError("Exactly 2,000 distinct baseline cases required")
    groups = defaultdict(list)
    for row in rows:
        if row["balance_rule"] not in RULES: raise ValueError("Unknown balancing rule")
        groups[row["balance_rule"]].append(row)
    if set(groups) != set(RULES): raise ValueError("All nine balancing rules required")
    def allocate(total):
        counts = {rule: len(group) * total // len(rows) for rule, group in groups.items()}
        order = sorted(groups, key=lambda rule: (-(len(groups[rule]) * total % len(rows)), rule))
        for rule in order[:total - sum(counts.values())]: counts[rule] += 1
        return counts
    disagree, uncertain = allocate(400), allocate(100)
    output = {}
    for rule, group in groups.items():
        ordered = sorted(group, key=lambda row: content_hash([METHOD, baseline_id, row["id"]]))
        for index, row in enumerate(ordered):
            output[row["id"]] = "DISAGREE" if index < disagree[rule] else "UNCERTAIN" if index < disagree[rule] + uncertain[rule] else "AGREE"
    return output


def project(verdict, mode):
    if verdict not in VERDICTS or mode not in MODES: raise ValueError("Unknown advisory verdict or assignment")
    if mode == "UNCERTAIN" or verdict == "INCONCLUSIVE": return "INCONCLUSIVE"
    if mode == "AGREE": return verdict
    return "FALSE_POSITIVE" if verdict == "TRUE_POSITIVE" else "TRUE_POSITIVE"


def build(rows, advisories, baseline_id):
    modes = assignments(rows, baseline_id)
    by_case = {row["case_id"]: row for row in advisories}
    if len(by_case) != len(advisories) or set(by_case) != set(modes): raise ValueError("Complete, unique, baseline-only advisory coverage required")
    records = []
    for row in rows:
        advisory = by_case[row["id"]]
        if (advisory.get("baseline_id") != baseline_id or advisory.get("dataset_id") != SEEDED_ID
            or advisory.get("provenance") not in {"REAL_PROVIDER_AI_ADVISORY_NOT_HUMAN", "LOCAL_AGENT_DRAFT_ADVISORY_NOT_HUMAN"}
            or advisory.get("human_review") is not False): raise ValueError("Invalid advisory provenance")
        mapping, reviews = advisory["alert_mapping"], advisory["reviews"]
        source = corpus().case(row["id"])
        expected = {(alert["id"], alert["rule_id"]) for alert in source["alerts"]}
        if (len(mapping) != len(expected) or {(alert["alert_id"], alert["rule_id"]) for alert in mapping} != expected
            or len({alert["alert_ref"] for alert in mapping}) != len(mapping)
            or len(reviews) != len(mapping) or {review["alert_ref"] for review in reviews} != {alert["alert_ref"] for alert in mapping}):
            raise ValueError("Advisory mapping differs from source alerts")
        evidence = source["evidence"]
        manual = bool(evidence["protected_signals"] or evidence["missing_context"])
        if advisory.get("requires_manual_review") != manual: raise ValueError("Manual-review flag changed")
        by_ref = {review["alert_ref"]: review for review in reviews}
        opinions = []
        for alert in mapping:
            review = by_ref[alert["alert_ref"]]
            if not review.get("evidence_ids") or not set(review["evidence_ids"]) <= set(evidence["evidence_ids"]): raise ValueError("Advisory cites another case")
            verdict = project(review["verdict"], modes[row["id"]])
            opinions.append({"alert_id": alert["alert_id"], "rule_id": alert["rule_id"],
                "source_advisory_verdict": review["verdict"], "projected_verdict": verdict,
                "unresolved_disagreement": modes[row["id"]] == "DISAGREE" and review["verdict"] == "INCONCLUSIVE"})
        body = {"baseline_id": baseline_id, "method": METHOD, "provenance": PROVENANCE,
            "case_id": row["id"], "identity_id": row["identity_id"], "balance_rule": row["balance_rule"],
            "source_advisory_id": advisory["id"], "source_advisory_hash": content_hash(advisory),
            "source_advisory_provenance": advisory["provenance"], "assignment": modes[row["id"]],
            "human_review": False, "human_verified": False, "requires_manual_review": manual,
            "heuristic_score": row["heuristic_score"], "opinions": opinions}
        ident = content_hash(body)
        records.append({"id": ident, "baseline_id": baseline_id, "body": body, "content_hash": ident})
    return records


def aggregate(records, baseline_id):
    counts = Counter(record["body"]["assignment"] for record in records)
    statistics, fitting = [], []
    overall = sum(any(opinion["projected_verdict"] != "INCONCLUSIVE" for opinion in record["body"]["opinions"]) for record in records)
    for rule in RULES:
        selected = [(record["body"], opinion) for record in records for opinion in record["body"]["opinions"] if opinion["rule_id"] == rule]
        verdicts = Counter(opinion["projected_verdict"] for _, opinion in selected)
        comparable = [(body, opinion) for body, opinion in selected if opinion["source_advisory_verdict"] != "INCONCLUSIVE" and opinion["projected_verdict"] != "INCONCLUSIVE"]
        observations = [{"identity_id": body["identity_id"], "case_id": body["case_id"], "score": body["heuristic_score"], "verdict": opinion["projected_verdict"]}
            for body, opinion in selected if not body["requires_manual_review"] and opinion["projected_verdict"] != "INCONCLUSIVE"]
        candidate = fit(observations, {"tp": .25, "fp": .65}, overall)
        statistics.append({"rule_id": rule, "cases": len(selected), "true_positive": verdicts["TRUE_POSITIVE"], "false_positive": verdicts["FALSE_POSITIVE"],
            "tp_cutoff": .25, "fp_cutoff": .65,
            "inconclusive": verdicts["INCONCLUSIVE"], "comparable": len(comparable),
            "agreement": sum(opinion["source_advisory_verdict"] == opinion["projected_verdict"] for _, opinion in comparable),
            "uncertain": sum(opinion["source_advisory_verdict"] == "INCONCLUSIVE" for _, opinion in selected),
            "assignment_counts": dict(Counter(body["assignment"] for body, _ in selected)),
            "balancing_assignment_counts": dict(Counter(record["body"]["assignment"] for record in records if record["body"]["balance_rule"] == rule))})
        fitting.append({"rule_id": rule, "eligible_observations": len(observations), "candidate": candidate,
            "status": "UNVALIDATED_SYNTHETIC_CANDIDATE" if candidate else "NO_SUPPORTED_CANDIDATE", "validation": "NOT_PERFORMED", "operationally_applicable": False})
    return {"baseline_id": baseline_id, "method": METHOD, "provenance": PROVENANCE, "cases": len(records),
        "assignment_counts": dict(counts), "human_reviews_created": 0, "model_calls": 0, "rules": statistics,
        "fitting_results": fitting, "note": "Simulated analyst reactions, not human-verified outcomes or measured improvement. Case-level assignments are 75/20/5; inconclusive advisories remain uncertain, so final uncertainty may exceed 5%. Rule alert counts overlap."}


def prepare(repo, *, apply=False):
    if repo.settings.mode != "local" or repo.settings.project_id != "demo-aml-insight": raise Conflict("Projections are local-emulator-only")
    active = workspace.view(repo)
    if not active: raise Conflict("Active baseline required")
    rows = [row for row in workspace.rows(repo, active) if row["reference"]]
    advisories = [row for row in repo.list("seeded_advisory_reviews", SEEDED_ID) if row["baseline_id"] == active["baseline_id"]]
    records = build(rows, advisories, active["baseline_id"])
    body = aggregate(records, active["baseline_id"])
    body["records"] = [{"id": record["id"], "case_id": record["body"]["case_id"]} for record in sorted(records, key=lambda record: record["id"])]
    run_id = content_hash(body)
    existing = {row["id"]: row for row in repo.list("projected_analyst_reviews", active["baseline_id"])}
    for record in records:
        previous = existing.get(record["id"])
        if previous and (previous.get("body") != record["body"] or previous.get("content_hash") != record["id"]): raise Conflict("Projection changed")
    previous_run = repo.get("projected_review_runs", run_id)
    if previous_run and (previous_run.get("body") != body or previous_run.get("content_hash") != run_id): raise Conflict("Projection manifest changed")
    missing = [record for record in records if record["id"] not in existing]
    if apply:
        # The completed manifest/pointer is published only after all derivative rows exist.
        for at in range(0, len(missing), 400):
            batch = repo.db.batch()
            for record in missing[at:at + 400]:
                batch.create(repo.db.collection("projected_analyst_reviews").document(record["id"]), {**record, "dataset_id": active["baseline_id"]})
            batch.commit(timeout=30)
        if not previous_run:
            try: repo.create("projected_review_runs", run_id, {"dataset_id": active["baseline_id"], "body": body, "content_hash": run_id})
            except AlreadyExists: pass
        repo.db.collection("projected_review_controls").document(active["baseline_id"]).set({"run_id": run_id})
    return {key: value for key, value in body.items() if key != "records"} | {"run_id": run_id, "apply": apply, "created": len(missing) if apply else 0, "would_create": len(missing)}


def summary(repo):
    baseline_id = workspace.selected_id(repo)
    pointer = repo.get("projected_review_controls", baseline_id) if baseline_id else None
    if not pointer: return {"dataset_id": baseline_id, "prepared": False}
    run = repo.get("projected_review_runs", pointer["run_id"])
    if not run or run.get("dataset_id") != baseline_id or content_hash(run["body"]) != pointer["run_id"]: raise Conflict("Projection manifest changed")
    return {"dataset_id": baseline_id, "prepared": True, "run_id": pointer["run_id"], **{key: value for key, value in run["body"].items() if key != "records"}}
