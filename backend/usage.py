"""Usage projections from step_runs, never a second ledger."""
TOKEN_FIELDS = ("prompt_tokens", "cached_prompt_tokens", "candidate_tokens", "thought_tokens", "tool_prompt_tokens", "total_tokens")
ATTEMPT_FIELDS = ("id", "run_id", "leg_id", "attempt_number", "retry_of_step_id", "model_id", "state", "error_code",
    "duration_ms", "context_precount_tokens", "usage_status", "cost_status", "estimated_usd_micros", "rate_card_id", "validation") + TOKEN_FIELDS
RUN_FIELDS = ("id", "kind", "parent_run_id", "case_id", "alert_id", "state", "stage", "error_code", "child_run_ids", "started_at", "completed_at")


def attempt_summary(step):
    result = {field: step.get(field) for field in ATTEMPT_FIELDS}
    result["validation"] = {key: step.get("validation", {}).get(key) for key in ("schema_pass", "evidence_pass", "validation_version", "error_types", "missing_fields", "provider_rejection")}
    return result


def run_summary(run):
    return {field: run.get(field) for field in RUN_FIELDS}


def reconcile(step):
    if step.get("usage_status") != "PROVIDER_REPORTED": return None
    values = [step.get(key) for key in TOKEN_FIELDS]
    if any(type(value) is not int or value < 0 for value in values): return None
    prompt, cached, candidate, thought, tool, total = values
    if cached > prompt or prompt + candidate + thought + tool != total: return None
    return {"uncached_prompt_tokens": prompt - cached, "cached_prompt_tokens": cached,
        "candidate_tokens": candidate, "thought_tokens": thought, "tool_prompt_tokens": tool}


def summarize(steps):
    steps = list(steps)
    provider = [step for step in steps if step.get("usage_status") == "PROVIDER_REPORTED"]
    categories = [reconcile(step) for step in provider]
    reconciled = bool(provider) and all(value is not None for value in categories)
    return {"categories": {key: sum(value[key] for value in categories) for key in categories[0]} if reconciled else None,
        "total_tokens": sum(step["total_tokens"] for step in provider if type(step.get("total_tokens")) is int) if any(type(s.get("total_tokens")) is int for s in provider) else None,
        "known_estimated_usd_micros": sum(step["estimated_usd_micros"] for step in steps if step.get("cost_status") == "ESTIMATED" and type(step.get("estimated_usd_micros")) is int),
        "unknown_attempts": sum(step.get("usage_status") == "UNKNOWN" or step.get("cost_status") == "UNKNOWN" for step in steps),
        "not_applicable_attempts": sum(step.get("usage_status") == "NOT_APPLICABLE" for step in steps),
        "provider_attempts": sum(step.get("usage_status") != "NOT_APPLICABLE" for step in steps),
        "known_categories": {key: sum(step[key] for step in provider if type(step.get(key)) is int) if any(type(s.get(key)) is int for s in provider) else None for key in TOKEN_FIELDS}}
