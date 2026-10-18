"""Versioned demo rules and A1/A2 arithmetic. No model or label access."""
from datetime import timedelta
from statistics import mean, pstdev

from .dataset import stable_id
from .models import Case, Dataset, Split

RULES = {
    "HIGH_VALUE": "High-value movement", "CRYPTO": "Cryptocurrency activity",
    "PEP": "Politically exposed customer", "POST_DEATH": "Post-death activity",
    "DORMANT": "Dormant account reactivation", "MULTI_CURRENCY": "Multiple currencies",
    "CASH_INTENSITY": "High cash intensity", "FOREIGN_WIRE": "Foreign-currency wire",
    "STRUCTURING": "Repeated sub-threshold cash deposits",
}
WEIGHTS = {"transaction": .30, "customer": .25, "account": .15, "behavioral": .20, "network": .10}
PROTECTED = {"SANCTIONS", "POST_DEATH", "STRUCTURING"}


def enrich(data: Dataset, case: Case) -> dict:
    trigger = data.transactions[case.trigger_transaction_id]
    customer = data.customers[case.customer_id]
    account = data.accounts[trigger.account_id]
    at = trigger.occurred_at
    history = sorted((t for t in data.transactions.values() if t.customer_id == customer.id and at - timedelta(days=90) < t.occurred_at < at), key=lambda t: (t.occurred_at, t.id))
    # Triggers at the same timestamp are excluded from pre-trigger historical baselines.
    recent = [t for t in history if t.occurred_at > at - timedelta(days=30)] + [trigger]
    previous = [t for t in history if at - timedelta(days=60) < t.occurred_at <= at - timedelta(days=30)]
    prior = [t for t in history if t.account_id == account.id]
    last_activity = prior[-1].occurred_at if prior else account.prior_activity_at
    amounts = [t.amount_aud_minor for t in prior if t.type == trigger.type and t.currency == trigger.currency]
    volume = sum(t.amount_aud_minor for t in recent)
    cash = sum(t.amount_aud_minor for t in recent if t.type == "CASH")
    short_cash = [t for t in recent if t.type == "CASH" and t.direction == "CREDIT" and at - timedelta(hours=72) <= t.occurred_at and 800_000 <= t.amount_aud_minor < 1_000_000]
    missing = list(case.data_quality)
    if account.history_complete_from is None: missing.append("History coverage is unavailable")
    elif account.history_complete_from > at - timedelta(days=30): missing.append("History does not cover the full 30-day window")
    if trigger.counterparty_id is None: missing.append("Counterparty is unavailable")
    if customer.pep is None or customer.synthetic_sanctions_flag is None: missing.append("Customer flags are unavailable")
    if trigger.counterparty_sanctions_flag is None: missing.append("Synthetic counterparty screening context is unavailable")
    if last_activity is None: missing.append("Previous activity is unavailable")
    signals = []
    if trigger.amount_aud_minor > 1_000_000: signals.append("HIGH_VALUE")
    if trigger.type == "CRYPTO": signals.append("CRYPTO")
    if customer.pep: signals.append("PEP")
    if customer.synthetic_sanctions_flag or trigger.counterparty_sanctions_flag: signals.append("SANCTIONS")
    if customer.synthetic_sanctions_flag: signals.append("CUSTOMER_SANCTIONS")
    if trigger.counterparty_sanctions_flag: signals.append("COUNTERPARTY_SANCTIONS")
    if customer.deceased_at and trigger.occurred_at > customer.deceased_at: signals.append("POST_DEATH")
    if last_activity and at - last_activity >= timedelta(days=365): signals.append("DORMANT")
    if len({t.currency for t in recent}) >= 3: signals.append("MULTI_CURRENCY")
    if volume > 0 and cash / volume > .5: signals.append("CASH_INTENSITY")
    if trigger.type == "WIRE" and trigger.currency != "AUD": signals.append("FOREIGN_WIRE")
    if len(short_cash) >= 3 and sum(t.amount_aud_minor for t in short_cash) > 2_000_000: signals.append("STRUCTURING")
    if len(previous) > 0 and len(recent) >= 3 * len(previous): signals.append("VELOCITY")
    known_counterparties = {t.counterparty_id for t in history if t.occurred_at <= at - timedelta(hours=24)}
    new_counterparties = {t.counterparty_id for t in recent if t.counterparty_id and t.occurred_at > at - timedelta(hours=24) and t.counterparty_id not in known_counterparties}
    if len(new_counterparties) >= 3: signals.append("NEW_COUNTERPARTIES")
    if at - customer.onboarded_at < timedelta(days=180): signals.append("NEW_CUSTOMER")
    if at - account.opened_at < timedelta(days=90): signals.append("NEW_ACCOUNT")
    return {
        "case_id": case.id, "customer": customer.model_dump(mode="json"),
        "account": account.model_dump(mode="json"), "trigger": trigger.model_dump(mode="json"),
        "history": [t.model_dump(mode="json") for t in history], "signals": signals,
        "missing_context": sorted(set(missing)), "protected_signals": sorted(PROTECTED.intersection(signals)),
        "facts": {"window_count": len(recent), "window_volume_aud_minor": volume,
            "cash_ratio": cash / volume if volume else None, "currency_count": len({t.currency for t in recent}),
            "previous_window_count": len(previous), "last_activity_at": last_activity.isoformat() if last_activity else None,
            "history_count": len(history), "comparable_count": len(amounts),
            "historical_mean_aud_minor": mean(amounts) if amounts else None,
            "historical_stddev_aud_minor": pstdev(amounts) if amounts else None},
        "evidence_ids": [customer.id, account.id, trigger.id] + [t.id for t in history],
        "provenance": "DETERMINISTIC_SOURCE_FACTS",
    }


def risk_assessment(evidence: dict) -> dict:
    signals = set(evidence["signals"])
    policies = {
        "transaction": {"STRUCTURING": 1., "HIGH_VALUE": .7, "CASH_INTENSITY": .6, "CRYPTO": .5},
        "customer": {"CUSTOMER_SANCTIONS": .9, "PEP": .6, "NEW_CUSTOMER": .2},
        "account": {"POST_DEATH": .9, "DORMANT": .8, "NEW_ACCOUNT": .3},
        "behavioral": {"STRUCTURING": .8, "VELOCITY": .6, "MULTI_CURRENCY": .5},
        "network": {"COUNTERPARTY_SANCTIONS": .9, "NEW_COUNTERPARTIES": .7, "FOREIGN_WIRE": .4},
    }
    dimensions = {name: max([.1] + [value for signal, value in policy.items() if signal in signals]) for name, policy in policies.items()}
    if evidence["missing_context"]:
        return {"dimensions": {k: None for k in WEIGHTS}, "weights": WEIGHTS, "overall_score": None, "tier": "UNAVAILABLE", "policy_version": "risk-v1"}
    total = round(sum(dimensions[key] * weight for key, weight in WEIGHTS.items()), 6)
    tier = "LOW" if total < .3 else "MEDIUM" if total < .5 else "HIGH" if total < .7 else "CRITICAL"
    return {"dimensions": dimensions, "weights": WEIGHTS, "overall_score": total, "tier": tier, "policy_version": "risk-v1"}


def prioritize(evidence: dict, risk: dict, action: str | None, valid: bool = True) -> dict:
    if not valid or evidence["missing_context"]: priority, reason = "NEEDS_CONTEXT", "Missing context or failed output validation"
    elif evidence["protected_signals"] or risk["tier"] in {"HIGH", "CRITICAL"} or action == "ESCALATE": priority, reason = "URGENT_REVIEW", "Protected signal, elevated score or escalation suggestion"
    elif risk["tier"] == "LOW" and action == "CLOSE": priority, reason = "LOWER_PRIORITY", "Complete low-risk context; human decision still required"
    else: priority, reason = "STANDARD_REVIEW", "Requires analyst review"
    return {"priority": priority, "reason": reason, "is_verdict": False}


AUTO_LABEL_VERSION = "auto-label-v1"


def auto_label(evidence, classification, thresholds=None, policy_version=AUTO_LABEL_VERSION):
    signals = []
    if classification["primary_typology"] == "LOW_RISK": signals.append(("LOW_RISK", .5))
    if classification["suggested_action"] == "CLOSE": signals.append(("CLOSE", .3))
    if classification["uncertainty"] == "HIGH": signals.append(("HIGH_UNCERTAINTY", .2))
    score = round(sum(value for _, value in signals), 2)
    forced = bool(evidence["missing_context"] or evidence["protected_signals"])
    thresholds = thresholds or {"tp": .25, "fp": .65}
    if not 0 <= thresholds["tp"] < thresholds["fp"] <= 1: raise ValueError("Invalid A0 thresholds")
    label = "AUTO_UNCERTAIN" if forced else "AUTO_FP" if score >= thresholds["fp"] else "AUTO_TP" if score <= thresholds["tp"] else "AUTO_UNCERTAIN"
    return {"predicted_verdict": label, "heuristic_score": score, "label_policy_version": policy_version,
        "label_signals": [name for name, _ in signals], "requires_context_or_protected_review": forced,
        "score_is_probability": False}


def retains_high_value(evidence, candidate):
    if not candidate: return True
    if candidate not in candidate_options(): raise ValueError("Unsupported detector candidate")
    trigger, facts = evidence["trigger"], evidence["facts"]
    if candidate["kind"] == "RULE_THRESHOLD": return trigger["amount_aud_minor"] > candidate["threshold_aud_minor"]
    from datetime import datetime
    return not (not evidence["missing_context"] and not evidence["protected_signals"]
        and datetime.fromisoformat(trigger["occurred_at"]) - datetime.fromisoformat(evidence["account"]["opened_at"]) >= timedelta(days=365)
        and facts["comparable_count"] >= 30
        and abs(trigger["amount_aud_minor"] - facts["historical_mean_aud_minor"]) <= 2 * facts["historical_stddev_aud_minor"])


def detect(data: Dataset, case: Case, candidate: dict | None = None) -> list[dict]:
    if candidate is not None and candidate not in candidate_options(): raise ValueError("Unsupported detector candidate")
    evidence = enrich(data, case)
    matches = [signal for signal in evidence["signals"] if signal in RULES]
    if candidate and "HIGH_VALUE" in matches and not retains_high_value(evidence, candidate):
        matches.remove("HIGH_VALUE")
    return [{"id": stable_id(data.id, case.trigger_transaction_id, rule), "dataset_id": data.id,
             "case_id": case.id, "trigger_transaction_id": case.trigger_transaction_id,
             "rule_id": rule, "rule_name": RULES[rule], "status": "PENDING", "feedback_revision": 0,
             "detector_version_id": "detector-v1", "current_run_id": None} for rule in matches]


def candidate_options() -> list[dict]:
    return [{"id": "hv-15000", "kind": "RULE_THRESHOLD", "threshold_aud_minor": 1_500_000},
            {"id": "hv-20000", "kind": "RULE_THRESHOLD", "threshold_aud_minor": 2_000_000},
            {"id": "expected-activity", "kind": "EXPECTED_ACTIVITY_EXEMPTION"}]


def simulate(data: Dataset, candidate: dict, split: str) -> dict:
    split = Split(split)
    if candidate not in candidate_options(): raise ValueError("Candidate must be selected from the versioned allowlist")
    baseline_counts = dict(TP=0, FP=0, TN=0, FN=0)
    candidate_counts = dict(TP=0, FP=0, TN=0, FN=0)
    removed_benign, removed_suspicious, retained_elsewhere, lost = 0, 0, [], []
    population = []
    for case in data.cases.values():
        if case.split != split: continue
        label = data.reference_labels[case.id]
        if label == "INCONCLUSIVE": continue
        population.append(case.id)
        before, after = detect(data, case), detect(data, case, candidate)
        suspicious = label == "SUSPICIOUS"
        for counts, positive in [(baseline_counts, bool(before)), (candidate_counts, bool(after))]:
            counts["TP" if suspicious and positive else "FN" if suspicious else "FP" if positive else "TN"] += 1
        removed = len(before) - len(after)
        if suspicious: removed_suspicious += removed
        else: removed_benign += removed
        if removed and after: retained_elsewhere.append(case.id)
        if before and not after and suspicious: lost.append(case.id)
    def metrics(counts):
        def ratio(n, d): return n / d if d else None
        return {**counts, "recall": ratio(counts["TP"], counts["TP"] + counts["FN"]),
            "precision": ratio(counts["TP"], counts["TP"] + counts["FP"]),
            "fpr": ratio(counts["FP"], counts["FP"] + counts["TN"])}
    return {"candidate": candidate, "split": split, "case_ids": population, "dataset_hash": data.manifest_hash,
        "baseline": metrics(baseline_counts), "candidate_metrics": metrics(candidate_counts),
        "benign_alerts_removed": removed_benign, "suspicious_alerts_removed": removed_suspicious,
        "cases_retained_by_other_rules": retained_elsewhere, "additional_missed_case_ids": lost,
        "result": "RISK_DETECTED" if lost else "NO_ADDITIONAL_MISSES_OBSERVED" if population else "INSUFFICIENT_EVIDENCE",
        "provenance": "DETERMINISTIC_SYNTHETIC_REFERENCE_REPLAY", "activation_allowed": False}
