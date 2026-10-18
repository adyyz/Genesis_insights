"""A frozen, explicitly simulated walkthrough; never provider or human evidence."""
from collections import Counter, defaultdict, deque
from functools import lru_cache
import json

from .bulk_dataset import corpus
from .dataset import content_hash
from .engine import RULES

SIZE = 10000
PROVENANCE = "SYNTHETIC_REFERENCE_WALKTHROUGH"


@lru_cache(maxsize=1)
def cohort():
    data = corpus()
    with data.connect() as db, data.connect(True) as refs:
        labels = {r["case_id"]: r["verdict"] for r in refs.execute("SELECT * FROM annotations WHERE partition='TRAIN'")}
        rows = [dict(r) for r in db.execute("SELECT * FROM cases WHERE partition='TRAIN' ORDER BY ordinal")]
    buckets = defaultdict(deque)
    for row in rows:
        rules = json.loads(row["rules"])
        if not rules: continue
        # Allocate rare rules first; one case has one balancing stratum even if several rules trigger.
        primary = min(rules, key=lambda rule: (data.manifest["rule_alerts"][rule], rule))
        features = json.loads(row["features"])
        buckets[(primary, row["customer_type"], features["primary_typology"])].append(row)
    selected = []
    while len(selected) < SIZE and any(buckets.values()):
        for key in sorted(buckets):
            if len(selected) == SIZE: break
            if buckets[key]:
                row = buckets[key].popleft()
                safe = data.safe_row(row)
                label = "AUTO_UNCERTAIN" if row["forced"] else "AUTO_TP" if row["score"] <= .25 else "AUTO_FP" if row["score"] >= .65 else "AUTO_UNCERTAIN"
                verdict = {"BENIGN": "FALSE_POSITIVE", "SUSPICIOUS": "TRUE_POSITIVE", "INCONCLUSIVE": "INCONCLUSIVE"}[labels[row["id"]]]
                selected.append({**safe, "ordinal": row["ordinal"], "reference": True, "reference_verdict": verdict,
                    "reference_prediction": label, "reference_score": row["score"], "balance_rule": key[0],
                    "reference_typology": key[2], "provenance": PROVENANCE})
    if len(selected) != SIZE: raise ValueError("Insufficient training cases for the reference walkthrough")
    selected.sort(key=lambda row: row["ordinal"])
    return selected


@lru_cache(maxsize=1)
def summary():
    rows = cohort()
    def measured(items):
        counts = Counter(r["reference_verdict"] for r in items)
        comparable = [r for r in items if r["reference_verdict"] != "INCONCLUSIVE" and r["reference_prediction"] != "AUTO_UNCERTAIN"]
        agreement = sum((r["reference_prediction"] == "AUTO_TP") == (r["reference_verdict"] == "TRUE_POSITIVE") for r in comparable)
        return {"cases": len(items), "true_positive": counts["TRUE_POSITIVE"], "false_positive": counts["FALSE_POSITIVE"],
            "inconclusive": counts["INCONCLUSIVE"], "comparable": len(comparable), "agreement": agreement,
            "uncertain": sum(r["reference_prediction"] == "AUTO_UNCERTAIN" for r in items),
            "tp_cutoff": .25, "fp_cutoff": .65}
    return {"provenance": PROVENANCE, "method_version": "reference-walkthrough-v1", "cases": SIZE, "population_fraction": .25,
        "cohort_hash": content_hash(rows), "manifest_hash": corpus().manifest["manifest_hash"],
        "human_reviews": 0, "provider_calls": 0, "metrics": measured(rows),
        "rules": [{"rule_id": rule, **measured([r for r in rows if rule in r["rules"]]),
            "balancing_cases": sum(r["balance_rule"] == rule for r in rows)} for rule in RULES],
        "identity_types": dict(Counter(r["customer_type"] for r in rows)),
        "typologies": dict(Counter(r["reference_typology"] for r in rows)),
        "balance_note": "Round-robin by primary rule, identity type and simulated typology, capped by available source cases. Rule counts overlap; rare-rule capacity prevents exact equality."}


def selected(case_id):
    return next((r for r in cohort() if r["id"] == case_id), None)


def page(*, limit=50, cursor=0, identity="", customer_type="", rule="", query=""):
    rows = [r for r in cohort() if (not identity or r["identity_id"] == identity)
        and (not customer_type or r["customer_type"] == customer_type) and (not rule or rule in r["rules"])
        and query.casefold() in r["name"].casefold()]
    after = [r for r in rows if r["ordinal"] > cursor]
    return {"items": after[:limit], "total": len(rows), "next_cursor": after[limit - 1]["ordinal"] if len(after) > limit else None}


def merge(pending, *, limit=50, cursor=0, **filters):
    reference = page(limit=limit + 1, cursor=cursor, **filters)
    rows = sorted(reference["items"] + pending["items"], key=lambda r: r["ordinal"])
    has_more = len(rows) > limit or pending["next_cursor"] is not None or reference["next_cursor"] is not None
    return {**pending, "items": rows[:limit], "total": pending["total"] + reference["total"],
        "next_cursor": rows[limit - 1]["ordinal"] if has_more and len(rows) >= limit else None}
