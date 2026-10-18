"""Read-only projections of sealed agent records; never change historical output."""
from .engine import WEIGHTS


def advisory_view(output, risk):
    dimensions = (output or {}).get("advisory_dimensions")
    if not dimensions:
        return {"status": "NOT_RECORDED", "dimensions": None, "overall_score": None, "delta": None, "large_difference": False, "authoritative_score": risk.get("overall_score")}
    complete = set(dimensions) == set(WEIGHTS) and all(value is not None for value in dimensions.values())
    score = round(sum(dimensions[key] * weight for key, weight in WEIGHTS.items()), 6) if complete else None
    baseline = risk.get("overall_score")
    delta = round(score - baseline, 6) if score is not None and baseline is not None else None
    return {"status": "AI_ADVISORY", "dimensions": dimensions, "overall_score": score,
        "delta": delta, "large_difference": delta is not None and abs(delta) > .15,
        "authoritative_score": baseline, "score_is_probability": False}


def agent_trace(steps, run_id, legs=("A1", "A2", "A3", "A4", "A0")):
    from .usage import attempt_summary
    rows = [step for step in steps if step.get("run_id") == run_id]
    result = []
    for leg in legs:
        attempts = sorted((row for row in rows if row.get("leg_id") == leg), key=lambda row: row.get("attempt_number", 1))
        latest = attempts[-1] if attempts else {}
        valid = latest.get("state") == "SUCCEEDED" and latest.get("validation", {}).get("schema_pass") and latest.get("validation", {}).get("evidence_pass")
        result.append({"leg_id": leg, "state": latest.get("state", "NOT_STARTED"),
            "attempts": len(attempts), "error_code": latest.get("error_code"),
            "model_id": latest.get("model_id"), "provenance": latest.get("execution_provenance"),
            "validation": attempt_summary(latest)["validation"],
            "output": latest.get("output") if valid else None})
    return result
