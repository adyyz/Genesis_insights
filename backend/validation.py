"""Reject unsupported model outputs before they can become an analyst artifact."""
from pydantic import BaseModel

from .models import Categorization, EvolutionAssessment, Investigation, RiskExplanation


def validate_model_output(output: BaseModel, evidence: dict) -> dict:
    allowed = set(evidence["evidence_ids"])
    citations = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_ids":
                    if not item or set(item) - allowed: raise ValueError("Output cites missing or unknown source evidence")
                    citations.extend(item)
                else: visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)

    payload = output.model_dump(mode="json")
    visit(payload)
    if evidence.get("intelligence_version") == 2:
        if isinstance(output, RiskExplanation) and output.advisory_dimensions is None:
            raise ValueError("Versioned risk output requires advisory dimensions, including explicit unavailable values")
        if isinstance(output, Categorization) and "secondary_typologies" not in output.model_fields_set:
            raise ValueError("Versioned categorization requires a secondary-typology list, which may be empty")
        if isinstance(output, Investigation) and output.confidence is None:
            raise ValueError("Versioned brief requires evidence-support confidence and its reason")
        if isinstance(output, EvolutionAssessment) and output.report_version != 2:
            raise ValueError("Versioned Evolution output requires report version 2")
    if isinstance(output, RiskExplanation) and output.advisory_dimensions is not None:
        from .engine import WEIGHTS
        dimensions = output.advisory_dimensions.model_dump()
        if set(dimensions) != set(WEIGHTS) or any(value is not None and not 0 <= value <= 1 for value in dimensions.values()):
            raise ValueError("Advisory risk requires five bounded dimensions")
        if evidence.get("missing_context") and any(value is not None for value in dimensions.values()):
            raise ValueError("Missing context requires unavailable advisory scores")
    if isinstance(output, EvolutionAssessment):
        from .evolution import sufficiency
        gate = evidence["analytical_inputs"]["detector_gate"]
        actual = sufficiency(gate["conclusive_reviews"], gate["target_rule_reviews"])
        if (output.conclusive_reviews, output.target_rule_reviews) != (actual["conclusive_reviews"], actual["target_rule_reviews"]):
            raise ValueError("A5 cannot change computed sample sizes")
        proposed = output.status == "SUPPORTED_PROPOSAL"
        if actual["status"] == "INSUFFICIENT_EVIDENCE" and output.status != "INSUFFICIENT_EVIDENCE":
            raise ValueError("Insufficient reviews cannot support a detector proposal")
        if actual["status"] == "SUFFICIENT_EVIDENCE" and output.status == "INSUFFICIENT_EVIDENCE":
            raise ValueError("A5 must report the computed sufficiency decision")
        if proposed:
            support = output.support_feedback_ids
            allowed_support = {row["id"] for row in evidence["human_feedback"]
                if row["rule_id"] == "HIGH_VALUE" and row["verdict"] in {"TRUE_POSITIVE", "FALSE_POSITIVE"}}
            if not output.candidate_id or not output.hypothesis or not support or len(set(support)) != len(support) or set(support) - allowed_support:
                raise ValueError("Proposal requires one allowlisted candidate and genuine target-rule support")
        elif output.candidate_id is not None or output.hypothesis is not None or output.support_feedback_ids:
            raise ValueError("A result without a proposal cannot carry a candidate")
        feedback = {row["id"]: row for row in evidence["human_feedback"]}
        for finding in output.findings:
            if len(set(finding.support_feedback_ids)) != len(finding.support_feedback_ids): raise ValueError("Duplicate pattern support")
            for ident in finding.support_feedback_ids:
                row = feedback.get(ident)
                if not row or row["rule_id"] != finding.rule_id or row["verdict"] != "FALSE_POSITIVE":
                    raise ValueError("Root-cause hypotheses require genuine target-rule false-positive support")
            if any(ident not in feedback or feedback[ident]["rule_id"] != finding.rule_id for ident in finding.counter_feedback_ids):
                raise ValueError("Unknown counter-evidence")
            allowed_signals = set(evidence["analytical_inputs"].get("computed_statistics", {}).get("signal_codes", []))
            if set(finding.signal_codes) - allowed_signals: raise ValueError("Unknown signal codes")
        for item in output.recommendation_items:
            if any(ident not in feedback or feedback[ident]["rule_id"] != item.rule_id for ident in item.support_feedback_ids):
                raise ValueError("Recommendation cites unknown target-rule support")
            if item.kind == "DETECTOR_CHANGE":
                if not proposed or item.rule_id != "HIGH_VALUE" or item.candidate_id != output.candidate_id:
                    raise ValueError("Only the validated allowlisted HIGH_VALUE proposal is executable")
            elif item.candidate_id is not None: raise ValueError("Informational findings cannot carry executable candidates")
    if isinstance(output, Categorization):
        names = [item.typology for item in output.secondary_typologies]
        if len(set(names)) != len(names) or output.primary_typology in names:
            raise ValueError("Secondary typologies must be distinct from each other and the primary")
        if evidence["missing_context"] and (output.primary_typology == "LOW_RISK" or output.suggested_action == "CLOSE" or output.uncertainty != "HIGH"):
            raise ValueError("Incomplete context requires uncertain classification and human review")
        if evidence["protected_signals"] and (output.primary_typology == "LOW_RISK" or output.suggested_action == "CLOSE"):
            raise ValueError("Protected signals cannot be silently classified for closure")
    if isinstance(output, Investigation):
        if (output.confidence is None) != (output.confidence_reason is None):
            raise ValueError("Brief confidence requires an evidence-linked reason")
        def texts(value):
            if isinstance(value, str): return [value]
            if isinstance(value, dict): return [text for key, item in value.items() if key != "evidence_ids" for text in texts(item)]
            if isinstance(value, list): return [text for item in value for text in texts(item)]
            return []
        if len(" ".join(texts(payload)).split()) >= 500: raise ValueError("Investigation brief must be under 500 words")
    return {"output": payload, "citation_id_validity": True, "citation_count": len(citations),
        "semantic_support": "REQUIRES_MANUAL_REVIEW", "validation_version": "grounding-v1"}
