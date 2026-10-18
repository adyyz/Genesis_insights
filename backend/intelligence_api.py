"""Explicit operator actions for rule intelligence; public reads use reviewed publications."""
import re
from uuid import uuid4
from typing import Annotated
from fastapi import Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from google.cloud import firestore
from .dataset import content_hash
from .repository import Conflict, now
from .models import EvolutionRequest, RecommendationReviewRequest, PolicyChangeRequest, ReassessmentRequest
from . import calibration
from .rule_intelligence import catalog, matrix, operational_rows, statistics


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    report_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    executive_summary: str = Field(min_length=1, max_length=2000)


class WithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    notes: str = Field(min_length=1, max_length=2000)


def public_brief(repo):
    control = repo.get("evolution_publication_controls", repo.dataset_id) or {"revision": 0, "publication_id": None}
    publication = repo.get("evolution_publications", control["publication_id"]) if control.get("publication_id") else None
    if publication and (publication.get("dataset_id") != repo.dataset_id or content_hash(publication["body"]) != publication["id"]): raise Conflict("Public brief integrity failed")
    return {"revision": control["revision"], "publication": publication}


def publication_body(repo, request):
    detail = repo.evolution_detail(request.report_id)
    if detail["run"]["state"] != "COMPLETE" or not detail.get("result"): raise Conflict("Completed genuine report required")
    summary = request.executive_summary.strip()
    if not summary or len(summary.split()) > 120 or re.search(r"[a-f0-9]{24,64}|AIza|Bearer |PRIVATE KEY", summary):
        raise ValueError("Public summary must be at most 120 words and contain no record identifiers or credentials")
    if any(row.get("display_name", "").lower() in summary.lower() for row in repo.list("customers") if row.get("display_name")):
        raise ValueError("Public summary must not identify individual cases or customers")
    if hasattr(repo, "publication_names") and any(name.lower() in summary.lower() for name in repo.publication_names()):
        raise ValueError("Public summary must not identify individual cases or customers")
    snapshot = repo.get("analytical_snapshots", detail["analytical_snapshot_id"])
    if not snapshot or content_hash(snapshot["body"]) != detail["analytical_snapshot_id"]: raise Conflict("Frozen report inputs changed")
    from .models import EvolutionAssessment
    from .validation import validate_model_output
    validate_model_output(EvolutionAssessment.model_validate(detail["result"]), repo.evolution_context(detail["analytical_snapshot_id"]))
    computed = snapshot["body"].get("computed_statistics")
    if not computed: raise Conflict("Generate a versioned report with computed aggregate statistics before publishing")
    counts = {key: computed[key] for key in ("total_cases", "demo_cases", "total_alerts", "reviewed_cases", "reviewed_alerts", "true_positive", "false_positive", "inconclusive", "compared", "agreed", "suggestion_unavailable", "machine_uncertain")}
    body = {"executive_summary": summary, "counts": counts, "scope": computed.get("scope", "DEMO_AND_DISCOVERY"),
        "source_report_id": request.report_id, "sample_note": "Human decisions on reviewed synthetic alerts; not population accuracy or FPR."}
    return body


def publish(repo, request, actor):
    body = publication_body(repo, request)
    ident = content_hash(body)
    @firestore.transactional
    def write(tx):
        ref = repo.db.collection("evolution_publication_controls").document(repo.dataset_id)
        current = ref.get(transaction=tx)
        revision = current.to_dict()["revision"] if current.exists else 0
        version_ref = repo.db.collection("evolution_publications").document(ident)
        version = version_ref.get(transaction=tx)
        if revision != request.expected_revision: raise Conflict("Public brief changed; refresh")
        if version.exists and version.to_dict()["body"] != body: raise Conflict("Public version integrity failed")
        if not version.exists: tx.create(version_ref, {"id": ident, "dataset_id": repo.dataset_id, "body": body, "published_at": now()})
        tx.set(ref, {"revision": revision + 1, "publication_id": ident})
        tx.create(repo.db.collection("audit_events").document(uuid4().hex), {"dataset_id": repo.dataset_id,
            "action": "EVOLUTION_SUMMARY_PUBLISHED", "object_collection": "evolution_publications", "object_id": ident,
            "actor": actor, "occurred_at": now()})
        return {"publication_id": ident, "revision": revision + 1}
    return repo._contended_transaction(write)


def withdraw(repo, request, actor):
    @firestore.transactional
    def write(tx):
        ref = repo.db.collection("evolution_publication_controls").document(repo.dataset_id)
        current = ref.get(transaction=tx)
        row = current.to_dict() if current.exists else {"revision": 0}
        if row["revision"] != request.expected_revision: raise Conflict("Public brief changed; refresh")
        tx.set(ref, {"revision": row["revision"] + 1, "publication_id": None})
        tx.create(repo.db.collection("audit_events").document(uuid4().hex), {"dataset_id": repo.dataset_id,
            "action": "EVOLUTION_SUMMARY_WITHDRAWN", "object_collection": "evolution_publications",
            "object_id": row.get("publication_id"), "actor": actor, "notes": request.notes, "occurred_at": now()})
        return {"revision": row["revision"] + 1, "publication": None}
    return repo._contended_transaction(write)


def register(app, repo, operator, prefix, public_prefix):
    def invoke(call):
        try: return call()
        except LookupError as exc: raise HTTPException(404, str(exc))
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.get(public_prefix + "/rules")
    def public_rules(): return {"rules": catalog()}

    @app.get(f"{public_prefix}/brief")
    def published(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return public_brief(repo)

    @app.get(f"{prefix}/rule-matrix")
    def rule_matrix(_actor=Depends(operator)): return invoke(lambda: matrix(repo))

    @app.get(prefix + "/cases/{case_id}")
    def private_case(case_id: str, _actor=Depends(operator)):
        def read():
            from .bulk_api import private_case
            return private_case(repo, case_id)
        return invoke(read)

    @app.get(f"{prefix}/rules/{{rule_id}}/examples")
    def examples(rule_id: str, _actor=Depends(operator), limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")):
        cases, _, alerts, feedback, _ = operational_rows(repo)
        ids = {row["id"] for row in alerts if row["rule_id"] == rule_id}
        page = repo.page("feedback_events", limit, cursor, [("dataset_id", repo.dataset_id)], order="submitted_at")
        cases = {row["id"]: row for row in cases}
        return {**page, "items": [{"feedback": row, "case": cases[row["case_id"]]} for row in page["items"] if row["alert_id"] in ids]}

    @app.get(f"{prefix}/auto-label/policy")
    def label_policy(_actor=Depends(operator)): return calibration.active_label_policy(repo)

    @app.post(f"{prefix}/auto-label/calibrations")
    def prepare_labels(request: EvolutionRequest, actor=Depends(operator)):
        return invoke(lambda: calibration.prepare(repo, request.snapshot_id, actor))

    @app.get(f"{prefix}/auto-label/calibrations")
    def calibrations(_actor=Depends(operator), limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")):
        return repo.page("auto_label_calibrations", limit, cursor, [("dataset_id", repo.dataset_id)], order="created_at")

    @app.get(f"{prefix}/auto-label/candidates")
    def candidates(_actor=Depends(operator), limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")):
        page = repo.page("auto_label_candidates", limit, cursor, [("dataset_id", repo.dataset_id)], order="frozen_at")
        return {**page, "items": [calibration.candidate_summary(row) for row in page["items"]], "active_policy": calibration.active_label_policy(repo)}

    @app.post(f"{prefix}/auto-label/candidates/{{ident}}/validate")
    def validate(ident: str, actor=Depends(operator)): return invoke(lambda: calibration.candidate_summary(calibration.validate_candidate(repo, ident, actor)))

    @app.put(f"{prefix}/auto-label/candidates/{{ident}}/review")
    def review(ident: str, request: RecommendationReviewRequest, actor=Depends(operator)):
        return invoke(lambda: calibration.candidate_summary(calibration.decision(repo, ident, request, actor, "review")))

    @app.post(f"{prefix}/auto-label/candidates/{{ident}}/apply")
    def apply(ident: str, request: PolicyChangeRequest, actor=Depends(operator)):
        return invoke(lambda: calibration.candidate_summary(calibration.decision(repo, ident, request, actor, "apply")))

    @app.post(f"{prefix}/auto-label/candidates/{{ident}}/rollback")
    def rollback(ident: str, request: PolicyChangeRequest, actor=Depends(operator)):
        return invoke(lambda: calibration.candidate_summary(calibration.decision(repo, ident, request, actor, "rollback")))

    @app.post(f"{prefix}/auto-label/candidates/{{ident}}/reassess")
    def reassess(ident: str, request: ReassessmentRequest, actor=Depends(operator)):
        return invoke(lambda: calibration.reassess(repo, ident, request, actor))

    @app.get(f"{prefix}/auto-label/assessments")
    def assessments(_actor=Depends(operator), limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")):
        return repo.page("auto_label_assessments", limit, cursor, [("dataset_id", repo.dataset_id)], order="created_at")

    @app.get(f"{prefix}/public-brief")
    def brief(_actor=Depends(operator)): return public_brief(repo)

    @app.post(f"{prefix}/public-brief")
    def publication(request: PublicationRequest, actor=Depends(operator)): return invoke(lambda: publish(repo, request, actor))

    @app.post(f"{prefix}/public-brief/preview")
    def preview(request: PublicationRequest, _actor=Depends(operator)):
        return invoke(lambda: {"body": publication_body(repo, request)})

    @app.post(f"{prefix}/public-brief/withdraw")
    def withdrawal(request: WithdrawalRequest, actor=Depends(operator)): return withdraw(repo, request, actor)
