"""Deterministic analytical inputs; human reviews never come from predictions."""
from collections import Counter

from .dataset import content_hash
from .usage import summarize


def candidate_identity(manifest_hash, candidate):
    from .engine import candidate_options
    if candidate not in candidate_options(): raise ValueError("Candidate is not allowlisted")
    return content_hash({"dataset_hash": manifest_hash, "baseline": "detector-v1", "candidate": candidate})


def sufficiency(total, target):
    supported = total >= 30 and target >= 15
    return {"conclusive_reviews": total, "target_rule_reviews": target,
        "required_overall": 30, "required_target_rule": 15,
        "status": "SUFFICIENT_EVIDENCE" if supported else "INSUFFICIENT_EVIDENCE"}


def analytical_inputs(manifest_hash, feedback, steps, alerts, computed_statistics=None):
    """Inputs have already passed repository provenance checks.

    Revisions are retained for traceability, but only the latest verdict per
    alert counts. Repeated reviews of one alert are never additional samples.
    """
    alerts = {alert["id"]: alert for alert in alerts}
    latest = {}
    for event in sorted(feedback, key=lambda row: (row["revision"], row["id"])):
        latest[event["alert_id"]] = event
    conclusive = [event for event in latest.values() if event["verdict"] in {"TRUE_POSITIVE", "FALSE_POSITIVE"}]
    groups = {}
    for rule in sorted({alerts[event["alert_id"]]["rule_id"] for event in latest.values()}):
        reviews = [event for event in conclusive if alerts[event["alert_id"]]["rule_id"] == rule]
        counts = Counter(event["verdict"] for event in reviews)
        groups[rule] = {**sufficiency(len(conclusive), len(reviews)),
            "true_positive_reviews": counts["TRUE_POSITIVE"], "false_positive_reviews": counts["FALSE_POSITIVE"],
            "reviewed_alert_noise_proportion": counts["FALSE_POSITIVE"] / len(reviews) if reviews else None}
    succeeded = [step for step in steps if step["state"] == "SUCCEEDED"]
    classifications = {step["run_id"]: step["output"] for step in succeeded if step["leg_id"] == "A3" and step.get("output")}
    patterns = {}
    for event in conclusive:
        if event["verdict"] != "FALSE_POSITIVE": continue
        rule = alerts[event["alert_id"]]["rule_id"]
        typology = classifications.get(event.get("triage_run_id"), {}).get("primary_typology", "NOT_RECORDED")
        policy = event.get("detector_policy_id", "detector-v1")
        key = (rule, typology, policy)
        patterns.setdefault(key, []).append(event["id"])
    def references(rows):
        return [{"id": row["id"], "hash": content_hash(row)} for row in sorted(rows, key=lambda row: row["id"])]
    return {"version": "analytical-inputs-v2", "computed_statistics": computed_statistics or {}, "dataset_hash": manifest_hash,
        "feedback_policy": "LATEST_REVISION_PER_ALERT", "label_provenance": "HUMAN_OPERATOR_ONLY",
        "feedback_events": references(feedback), "latest_feedback_ids": sorted(event["id"] for event in latest.values()),
        "reviewed_alerts": references([{key: alerts[ident][key] for key in ("id", "rule_id")} for ident in sorted(latest)]),
        "succeeded_steps": references(succeeded), "usage_steps": references(steps),
        "config_bundle_ids": sorted({step["config_bundle_id"] for step in steps}),
        "sample_sizes": {"feedback_events": len(feedback), "reviewed_alerts": len(latest),
            "conclusive_reviews": len(conclusive), "inconclusive_reviews": len(latest) - len(conclusive),
            "succeeded_steps": len(succeeded), "usage_steps": len(steps)},
        "rules": groups, "detector_gate": sufficiency(len(conclusive), sum(alerts[event["alert_id"]]["rule_id"] == "HIGH_VALUE" for event in conclusive)),
        "usage": summarize(steps), "population_recall": None, "population_fpr": None,
        "population_metrics_status": "REVIEWED_ALERTS_ARE_NOT_A_FULL_POPULATION",
        "pattern_memory": [{"rule_id": rule, "typology": typology, "detector_policy_id": policy,
            "support_feedback_ids": sorted(set(ids)), "support_strength": min(len(set(ids)) / 15, 1),
            "status": "ACTIVE"} for (rule, typology, policy), ids in sorted(patterns.items())],
        "semantic_support": "REQUIRES_MANUAL_REVIEW", "activation_allowed": False}
