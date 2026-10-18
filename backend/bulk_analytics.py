"""Chunked, immutable bulk evidence; only bounded examples enter A5 prompts."""
from collections import Counter, defaultdict
from google.cloud import firestore
from google.api_core.exceptions import AlreadyExists
from .bulk_dataset import ID, corpus
from .dataset import content_hash
from .engine import RULES
from .evolution import analytical_inputs, sufficiency
from .repository import Repository, Conflict, now
from .usage import summarize


def pages(repo, collection, filters=(), size=500, *, dataset_id=ID):
    cursor = None
    while True:
        page = repo.page(collection, size, cursor, [("dataset_id", dataset_id), *filters])
        if page["items"]: yield page["items"]
        cursor = page["next_cursor"]
        if not cursor: break


def usage(repo, operator_summary=False, *, dataset_id=ID, source_pages=None):
    """Reduce bounded ledger pages. Unknown categories remain unknown."""
    totals, known, reported = Counter(), Counter(), set()
    chart = True; categories = Counter()
    failures = retries = duration_total = duration_count = 0
    for rows in source_pages if source_pages is not None else pages(repo, "step_runs", dataset_id=dataset_id):
        if operator_summary:
            failures += sum(row["state"] in {"FAILED", "UNKNOWN_OUTCOME"} for row in rows)
            retries += sum(row.get("attempt_number", 1) > 1 for row in rows)
            durations = [row["duration_ms"] for row in rows if row.get("duration_ms") is not None]
            duration_total += sum(durations); duration_count += len(durations)
        part = summarize(rows)
        totals["ledger_rows"] += len(rows)
        totals["succeeded_rows"] += sum(row["state"] == "SUCCEEDED" for row in rows)
        for key in ("known_estimated_usd_micros", "unknown_attempts", "not_applicable_attempts", "provider_attempts"):
            totals[key] += part[key]
        if part["total_tokens"] is not None: totals["total_tokens"] += part["total_tokens"]; reported.add("total_tokens")
        for key, value in part["known_categories"].items():
            if value is not None: known[key] += value; reported.add(key)
        if part["categories"] is None and any(row.get("usage_status") == "PROVIDER_REPORTED" for row in rows): chart = False
        elif part["categories"] is not None: categories.update(part["categories"])
    empty = summarize([])
    result = {**empty, **totals, "total_tokens": totals["total_tokens"] if "total_tokens" in reported else None,
        "known_categories": {key: known[key] if key in reported else None for key in empty["known_categories"]},
        "categories": dict(categories) if chart and categories else None}
    if operator_summary:
        result.update(failed_attempts=failures, retry_count=retries,
            mean_duration_ms=duration_total / duration_count if duration_count else None, daily=repo.daily_control())
    return result


class AnalyticsRepository(Repository):
    def __init__(self, settings): super().__init__(settings, ID)

    def operational_case(self, case_id):
        if hasattr(self, "workspace"):
            from .bulk_jobs import BulkRepository
            return BulkRepository(self.settings, case_id, self.dataset_id, self.db).operational_case(case_id)
        from .bulk_jobs import BulkRepository
        return BulkRepository(self.settings, case_id).operational_case(case_id)

    def operational_case_ids(self):
        if hasattr(self, "workspace"):
            from .baseline_workspace import rows
            return {row["id"] for row in rows(self, self.workspace)}
        with corpus().connect() as db:
            return {row[0] for row in db.execute("SELECT id FROM cases WHERE partition='PENDING'")}

    def operational_rows(self):
        if hasattr(self, "workspace"):
            from .baseline_workspace import rows
            sources = rows(self, self.workspace)
            cases = [{"id": row["id"], "customer_id": row["identity_id"], "split": "CALIBRATION_DISCOVERY"} for row in sources]
            customers = {row["identity_id"]: {"id": row["identity_id"], "customer_type": row["customer_type"]} for row in sources}
            ids = {row["id"] for row in cases}
            records = [[row for page in pages(self, collection, dataset_id=self.dataset_id) for row in page if row.get("case_id") in ids]
                for collection in ("alerts", "feedback_events", "step_runs")]
            return cases, list(customers.values()), *records
        with corpus().connect() as db:
            sources = list(db.execute("SELECT id,identity_id,customer_type FROM cases WHERE partition='PENDING'"))
        cases = [{"id": row["id"], "customer_id": row["identity_id"], "split": "CALIBRATION_DISCOVERY"} for row in sources]
        customers = {row["identity_id"]: {"id": row["identity_id"], "customer_type": row["customer_type"]} for row in sources}
        ids = {row["id"] for row in cases}
        alerts = [row for page in pages(self, "alerts") for row in page if row["case_id"] in ids]
        feedback = [row for page in pages(self, "feedback_events") for row in page if row.get("case_id") in ids]
        steps = [row for page in pages(self, "step_runs") for row in page if row.get("case_id") in ids]
        return cases, list(customers.values()), alerts, feedback, steps

    def source_alert_counts(self):
        if hasattr(self, "workspace"):
            from .baseline_workspace import rows
            sources = rows(self, self.workspace)
            return {rule: sum(rule in row["rules"] for row in sources) for rule in RULES}
        with corpus().connect() as db:
            return dict(db.execute("SELECT rule_id,count(*) FROM case_rules JOIN cases ON cases.id=case_rules.case_id WHERE partition='PENDING' GROUP BY rule_id"))

    def review_statistics(self, rows=None):
        from .review_metrics import review_summary
        result = review_summary(*(rows or self.operational_rows()))
        if hasattr(self, "workspace"):
            result.update(scope="ACTIVE_BASELINE_GENUINE_REVIEWS", dataset_id=self.dataset_id,
                total_cases=self.workspace["cases"], total_identities=self.workspace["identities"],
                operational_cases=self.workspace["reserved_cases"], reserved_cases=self.workspace["reserved_cases"], total_alerts=sum(self.source_alert_counts().values()),
                note="Genuine human reviews in the active Firestore baseline only. Gemini advisories, agent drafts and synthetic references are excluded. Agreement is not population accuracy or FPR.")
            return result
        result.update(scope="BULK_GENUINE_REVIEWS", total_cases=40000, total_identities=500,
            operational_cases=20000, total_alerts=sum(self.source_alert_counts().values()),
            note="Genuine human reviews from the 20,000 operational cases in the 40,000-case corpus. Training references and sealed validation are excluded. Agreement is not population accuracy or FPR.")
        return result

    def calibration_feedback(self, snapshot_id):
        self.evolution_context(snapshot_id)
        snapshot = self.get("analytical_snapshots", snapshot_id)
        events = []
        for reference in snapshot["body"].get("bulk_reference_chunks", []):
            chunk = self.get("bulk_evidence_chunks", reference["id"])
            if not chunk or len(chunk["references"]) != reference["count"] or content_hash(chunk["references"]) != reference["id"]:
                raise Conflict("Frozen evidence chunk changed")
            for item in chunk["references"]:
                event = self.get("feedback_events", item["id"])
                if (not event or event.get("dataset_id") != self.dataset_id or event.get("provenance") != "HUMAN_OPERATOR"
                    or content_hash(event) != item["hash"] or event["triage_run_id"] != item["run_id"] or event["alert_id"] != item["alert_id"]):
                    raise Conflict("Frozen bulk review changed")
                events.append(event)
        if len({event["alert_id"] for event in events}) != len(events) or len(events) != snapshot["body"]["sample_sizes"]["reviewed_alerts"]:
            raise Conflict("Frozen bulk review coverage changed")
        return events

    def publication_names(self):
        with corpus().connect() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT name FROM cases WHERE partition!='VALIDATION'")]

    def observed_effects(self, limit=50, cursor=None):
        from .calibration import active_label_policy
        policy = active_label_policy(self)
        page = self.page("auto_label_candidates", limit, cursor, [("dataset_id", self.dataset_id)], order="applied_at")
        candidates = page["items"]
        events = self.list("feedback_events")
        assessments = self.list("auto_label_assessments")
        rows = []
        for candidate in candidates:
            if not candidate.get("applied_at"): continue
            reviews = {event["alert_id"]: event for event in events if event.get("provenance") == "HUMAN_OPERATOR"
                and event.get("auto_label_policy_id") == candidate["applied_policy_id"] and event["submitted_at"] > candidate["applied_at"]
                and (self.get("alerts", event["alert_id"]) or {}).get("rule_id") == candidate["rule_id"]}
            related = [row for row in assessments if row["body"].get("candidate_id") == candidate["id"]]
            rows.append({"candidate_id": candidate["id"], "rule_id": candidate["rule_id"], "state": candidate["state"],
                "training": candidate["after"], "validation": candidate.get("validation"),
                "reassessed_alerts": len({row["body"]["alert_id"] for row in related if not row["body"].get("restoration")}),
                "restored_alerts": len({row["body"]["alert_id"] for row in related if row["body"].get("restoration")}),
                "reviewed_alerts": len(reviews), "true_positive": sum(event["verdict"] == "TRUE_POSITIVE" for event in reviews.values()),
                "false_positive": sum(event["verdict"] == "FALSE_POSITIVE" for event in reviews.values()),
                "inconclusive": sum(event["verdict"] == "INCONCLUSIVE" for event in reviews.values()),
                "status": "OBSERVED_REVIEWS" if reviews else "NOT_YET_OBSERVED"})
        return {"active_policy": policy, "items": rows, "next_cursor": page["next_cursor"], "note": "Observed reviews, not causal proof of improvement. Reassessed labels are not human reviews."}

    def freeze_analytical_inputs(self, actor):
        from .bulk_jobs import BulkRepository
        counts, groups = Counter(), defaultdict(Counter)
        patterns = {}
        chunks, examples, sample_steps, sample_alerts, cases, customers = [], [], [], [], {}, {}
        reviewed_cases = set()
        metric_totals, metric_groups = Counter(), defaultdict(lambda: defaultdict(Counter))
        cutoff = now()
        for events in pages(self, "feedback_events", dataset_id=self.dataset_id):
            references = []
            for event in events:
                if event["submitted_at"] > cutoff: continue
                if event.get("provenance") != "HUMAN_OPERATOR": raise Conflict("Bulk analytics accepts genuine human reviews only")
                worker = BulkRepository(self.settings, event["case_id"], self.dataset_id, self.db)
                alert = worker.get("alerts", event["alert_id"]); run = worker.get("pipeline_runs", event["triage_run_id"])
                if not alert or alert["dataset_id"] != self.dataset_id or not run or run["dataset_id"] != self.dataset_id or run["state"] != "COMPLETE" or run["alert_id"] != alert["id"]:
                    raise Conflict("Bulk feedback lineage failed")
                @firestore.transactional
                def chain(tx): return worker._sealed_chain(tx, run)
                steps = chain(self.db.transaction(read_only=True))
                if alert.get("feedback_revision") != event["revision"]: raise Conflict("Bulk feedback is not frozen")
                references.append({"id": event["id"], "hash": content_hash(event), "run_id": run["id"], "alert_id": alert["id"]})
                for pattern in analytical_inputs(corpus().manifest["manifest_hash"], [event], steps, [alert])["pattern_memory"]:
                    key = (pattern["rule_id"], pattern["typology"], pattern["detector_policy_id"])
                    patterns.setdefault(key, set()).update(pattern["support_feedback_ids"])
                counts[event["verdict"]] += 1; groups[alert["rule_id"]][event["verdict"]] += 1; reviewed_cases.add(event["case_id"])
                from .review_metrics import review_summary
                source = corpus().row(event["case_id"])
                one_case = {"id": event["case_id"], "customer_id": source["identity_id"], "split": "CALIBRATION_DISCOVERY"}
                one_customer = {"id": source["identity_id"], "customer_type": source["customer_type"]}
                one = review_summary([one_case], [one_customer], [alert], [event], steps)
                keys = ("reviewed_alerts", "true_positive", "false_positive", "inconclusive", "compared", "agreed", "suggestion_unavailable", "machine_uncertain")
                metric_totals.update({key: one[key] for key in keys})
                for dimension, values in one["groups"].items():
                    for value in values: metric_groups[dimension][value["name"]].update({key: value[key] for key in keys})
                if len(examples) < 3:
                    examples.append(event); sample_steps.extend(steps); sample_alerts.append(alert)
                    case = worker.operational_case(event["case_id"]); cases[case["id"]] = case
                    data = worker.load_dataset(); customers.update({k: v.model_dump() for k, v in data.customers.items()})
            if references:
                chunk_id = content_hash(references)
                try: self.create("bulk_evidence_chunks", chunk_id, {"dataset_id": self.dataset_id, "references": references})
                except AlreadyExists:
                    if self.get("bulk_evidence_chunks", chunk_id)["references"] != references: raise Conflict("Evidence chunk changed")
                chunks.append({"id": chunk_id, "count": len(references)})
        if not sum(counts.values()): raise Conflict("Genuine bulk analyst feedback is required; synthetic annotations do not count")
        from .rule_intelligence import statistics
        computed = statistics(list(cases.values()), list(customers.values()), sample_alerts, examples, sample_steps)
        total = sum(counts.values()); conclusive = counts["TRUE_POSITIVE"] + counts["FALSE_POSITIVE"]
        computed.update(total_cases=40000, demo_cases=0, total_identities=500, operational_cases=20000, total_alerts=sum(self.source_alert_counts().values()),
            reviewed_cases=len(reviewed_cases), reviewed_alerts=total, true_positive=counts["TRUE_POSITIVE"],
            false_positive=counts["FALSE_POSITIVE"], inconclusive=counts["INCONCLUSIVE"], scope="BULK_GENUINE_REVIEWS")
        if hasattr(self, "workspace"):
            computed.update(total_cases=self.workspace["cases"], total_identities=self.workspace["identities"],
                operational_cases=self.workspace["reserved_cases"], scope="ACTIVE_BASELINE_GENUINE_REVIEWS")
        computed.update(dict(metric_totals))
        computed["agreement_rate"] = metric_totals["agreed"] / metric_totals["compared"] if metric_totals["compared"] else None
        computed["groups"] = {dimension: [{"name": name, **dict(values),
            "agreement_rate": values["agreed"] / values["compared"] if values["compared"] else None} for name, values in grouped.items()]
            for dimension, grouped in metric_groups.items()}
        computed["signal_codes"] = [row["name"] for row in computed["groups"].get("signal", [])]
        body = analytical_inputs(corpus().manifest["manifest_hash"], examples, sample_steps, sample_alerts, computed)
        accounting = usage(self, dataset_id=self.dataset_id)
        body["sample_sizes"].update(succeeded_steps=accounting.pop("succeeded_rows", 0), usage_steps=accounting.pop("ledger_rows", 0))
        body["sample_sizes"].update(feedback_events=total, reviewed_alerts=total, conclusive_reviews=conclusive, inconclusive_reviews=counts["INCONCLUSIVE"])
        body.update(bulk_reference_chunks=chunks, feedback_examples_omitted=total - len(examples),
            rules={rule: {**sufficiency(conclusive, group["TRUE_POSITIVE"] + group["FALSE_POSITIVE"]),
                "true_positive_reviews": group["TRUE_POSITIVE"], "false_positive_reviews": group["FALSE_POSITIVE"],
                "reviewed_alert_noise_proportion": group["FALSE_POSITIVE"] / (group["TRUE_POSITIVE"] + group["FALSE_POSITIVE"]) if group["TRUE_POSITIVE"] + group["FALSE_POSITIVE"] else None}
                for rule in RULES for group in [groups[rule]]}, detector_gate=sufficiency(conclusive, groups["HIGH_VALUE"]["TRUE_POSITIVE"] + groups["HIGH_VALUE"]["FALSE_POSITIVE"]), usage=accounting)
        body["pattern_memory"] = [{"rule_id": key[0], "typology": key[1], "detector_policy_id": key[2],
            "support_feedback_ids": sorted(ids), "support_strength": min(len(ids) / 15, 1), "status": "ACTIVE"} for key, ids in sorted(patterns.items())]
        ident = content_hash(body)
        try: self.create("analytical_snapshots", ident, {"dataset_id": self.dataset_id, "content_hash": ident, "body": body, "created_by": actor})
        except AlreadyExists: pass
        self.record_patterns(ident)
        return self.get("analytical_snapshots", ident)

    def evolution_context(self, snapshot_id):
        snapshot = self.get("analytical_snapshots", snapshot_id)
        if snapshot:
            for reference in snapshot["body"].get("bulk_reference_chunks", []):
                chunk = self.get("bulk_evidence_chunks", reference["id"])
                if not chunk or content_hash(chunk["references"]) != reference["id"]: raise Conflict("Bulk evidence manifest changed")
                for item in chunk["references"]:
                    event = self.get("feedback_events", item["id"])
                    if not event or content_hash(event) != item["hash"]: raise Conflict("Frozen bulk review changed")
        return super().evolution_context(snapshot_id)

    def list(self, collection, dataset_id=None):
        if collection == "step_runs": return []  # A5 trace is fetched by run in evolution_detail below.
        return super().list(collection, dataset_id)

    def evolution_detail(self, run_id):
        from .agent_views import agent_trace
        result = super().evolution_detail(run_id)
        result["agent_trace"] = agent_trace(self.usage_steps(run_id), run_id, ("A5",))
        return result
