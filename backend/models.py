from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    dataset_id: str


class Split(StrEnum):
    DISCOVERY = "CALIBRATION_DISCOVERY"
    SELECTION = "CALIBRATION_SELECTION"
    HOLDOUT = "HOLDOUT"
    DEMO = "DEMO"


class Customer(Record):
    display_name: str
    customer_type: Literal["INDIVIDUAL", "ORGANIZATION"]
    onboarded_at: AwareDatetime
    pep: bool | None = False
    synthetic_sanctions_flag: bool | None = False
    deceased_at: AwareDatetime | None = None
    profile_context: str = Field(default="", max_length=2000)


class Account(Record):
    customer_id: str
    opened_at: AwareDatetime
    prior_activity_at: AwareDatetime | None
    history_complete_from: AwareDatetime | None
    currency: str = "AUD"
    status: Literal["OPEN", "CLOSED"] = "OPEN"


class Transaction(Record):
    account_id: str
    customer_id: str
    occurred_at: AwareDatetime
    original_minor: Annotated[StrictInt, Field(ge=0)]
    currency: Literal["AUD", "USD", "GBP", "EUR"]
    amount_aud_minor: Annotated[StrictInt, Field(ge=0)]
    fx_rate_id: str
    direction: Literal["CREDIT", "DEBIT"]
    type: Literal["CASH", "WIRE", "CRYPTO", "TRANSFER", "PAYMENT"]
    counterparty_id: str | None
    counterparty_sanctions_flag: bool | None = False
    description: str = Field(default="", max_length=2000)


class Case(Record):
    scenario_family: str
    split: Split
    customer_id: str
    account_ids: list[str]
    trigger_transaction_id: str
    history_transaction_ids: list[str]
    data_quality: list[str] = Field(default_factory=list)


class Dataset(BaseModel):
    id: str
    seed: int
    version: str
    customers: dict[str, Customer]
    accounts: dict[str, Account]
    transactions: dict[str, Transaction]
    cases: dict[str, Case]
    reference_labels: dict[str, Literal["SUSPICIOUS", "BENIGN", "INCONCLUSIVE"]]
    reference_reasons: dict[str, str]
    manifest_hash: str


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["TRUE_POSITIVE", "FALSE_POSITIVE", "INCONCLUSIVE"]
    notes: str = Field(min_length=1, max_length=2000)
    override_action: Literal["ESCALATE", "REVIEW", "MONITOR", "CLOSE"] | None = None
    expected_revision: int = Field(ge=0)
    expected_assessment_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1500)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)


class AdvisoryDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction: Annotated[float, Field(strict=True, ge=0, le=1)] | None
    customer: Annotated[float, Field(strict=True, ge=0, le=1)] | None
    account: Annotated[float, Field(strict=True, ge=0, le=1)] | None
    behavioral: Annotated[float, Field(strict=True, ge=0, le=1)] | None
    network: Annotated[float, Field(strict=True, ge=0, le=1)] | None


class RiskExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[Finding] = Field(max_length=8)
    counter_evidence: list[Finding] = Field(max_length=5)
    limitations: list[str] = Field(max_length=8)
    advisory_dimensions: AdvisoryDimensions | None = None


class SecondaryTypology(BaseModel):
    model_config = ConfigDict(extra="forbid")
    typology: Literal["STRUCTURING", "LAYERING", "SANCTIONS_EXPOSURE", "POTENTIAL_IDENTITY_MISUSE", "PEP_REVIEW", "OTHER"]
    rationale: Finding


class Categorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_typology: Literal["STRUCTURING", "LAYERING", "SANCTIONS_EXPOSURE", "POTENTIAL_IDENTITY_MISUSE", "PEP_REVIEW", "LOW_RISK", "OTHER", "UNDETERMINED"]
    suggested_action: Literal["ESCALATE", "REVIEW", "MONITOR", "CLOSE"]
    rationale: Finding
    uncertainty: Literal["LOW", "MEDIUM", "HIGH"]
    secondary_typologies: list[SecondaryTypology] = Field(default_factory=list, max_length=3)


class Investigation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overview: Finding
    key_findings: list[Finding] = Field(max_length=8)
    counter_evidence: list[Finding] = Field(max_length=5)
    missing_information: list[str] = Field(max_length=8)
    next_steps: list[str] = Field(max_length=8)
    limitations: list[str] = Field(max_length=8)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    confidence_reason: Finding | None = None


class TriageRequest(BaseModel):
    expected_policy_version: str | None = Field(default=None, pattern=r"^(?:detector-v1|[a-f0-9]{64})$")
    expected_auto_label_policy_version: str | None = Field(default=None, pattern=r"^(?:auto-label-v1|[a-f0-9]{64})$")
    model_config = ConfigDict(extra="forbid")
    alert_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{24}$")]] = Field(min_length=1, max_length=9)


class EvolutionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: Literal["HIGH_VALUE", "CRYPTO", "PEP", "POST_DEATH", "DORMANT", "MULTI_CURRENCY", "CASH_INTENSITY", "FOREIGN_WIRE", "STRUCTURING"]
    title: str = Field(min_length=1, max_length=120)
    explanation: str = Field(min_length=1, max_length=800)
    support_feedback_ids: list[str] = Field(min_length=1, max_length=10)
    counter_feedback_ids: list[str] = Field(default_factory=list, max_length=10)
    signal_codes: list[str] = Field(default_factory=list, max_length=10)


class EvolutionRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: EvolutionFinding.model_fields["rule_id"].annotation
    title: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=800)
    support_feedback_ids: list[str] = Field(default_factory=list, max_length=10)
    kind: Literal["DETECTOR_CHANGE", "INFORMATIONAL"]
    candidate_id: Literal["hv-15000", "hv-20000", "expected-activity"] | None = None


class EvolutionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["INSUFFICIENT_EVIDENCE", "NO_PROPOSAL", "SUPPORTED_PROPOSAL"]
    conclusive_reviews: Annotated[StrictInt, Field(ge=0)]
    target_rule_reviews: Annotated[StrictInt, Field(ge=0)]
    target_rule: Literal["HIGH_VALUE"]
    summary: str = Field(min_length=1, max_length=2000)
    candidate_id: Literal["hv-15000", "hv-20000", "expected-activity"] | None = None
    hypothesis: str | None = Field(default=None, min_length=1, max_length=1500)
    support_feedback_ids: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(max_length=8)
    report_version: Literal[1, 2] = 1
    findings: list[EvolutionFinding] = Field(default_factory=list, max_length=5)
    recommendation_items: list[EvolutionRecommendation] = Field(default_factory=list, max_length=5)


class EvolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReassessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    expected_policy_version: str = Field(pattern=r"^(auto-label-v1|[a-f0-9]{64})$")
    notes: str = Field(min_length=1, max_length=2000)
    cursor: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")


class CandidateFreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evolution_run_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    candidate_id: Literal["hv-15000", "hv-20000", "expected-activity"]
    expected_candidate_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    notes: str = Field(min_length=1, max_length=2000)


class RecommendationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    state: Literal["APPROVED", "REJECTED"]
    notes: str = Field(min_length=1, max_length=2000)


class DailyControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    live_enabled: bool
    limit: Annotated[StrictInt, Field(ge=1, le=120)]


class WorkerLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    generation: Annotated[StrictInt, Field(ge=1)]
    owner: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime
    execution_deadline_at: AwareDatetime


class PipelineRun(Record):
    kind: Literal["TRIAGE_BATCH", "TRIAGE_ALERT", "EVOLUTION", "EVALUATION", "SIMULATION"]
    parent_run_id: str | None = None
    alert_id: str | None = None
    case_id: str | None = None
    config_bundle_id: str
    analytical_snapshot_id: str | None = None
    state: Literal["QUEUED", "DISPATCH_FAILED", "RUNNING", "COMPLETE", "FAILED"] = "QUEUED"
    stage: str | None = None
    child_run_ids: list[str] = Field(default_factory=list, max_length=9)
    actor_uid: str
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    checkpoint_step_id: str | None = None
    error_code: str | None = None
    dispatch_generation: Annotated[StrictInt, Field(ge=0)] = 0
    dispatch_state: Literal["NOT_DISPATCHED", "PENDING", "SENT", "UNKNOWN", "FAILED"] = "NOT_DISPATCHED"
    dispatch_task_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: AwareDatetime | None = None
    execution_deadline_at: AwareDatetime | None = None
    lease_claim_count: Annotated[StrictInt, Field(ge=0)] = 0


class StepRun(Record):
    run_id: str
    alert_id: str | None = None
    case_id: str | None = None
    leg_id: Literal["A1", "A2", "A3", "A4", "A0", "A5", "AGGREGATE", "SIMULATE", "EVALUATE"]
    attempt_number: Annotated[StrictInt, Field(ge=1)] = 1
    retry_of_step_id: str | None = None
    retry_delay_seconds: Annotated[float, Field(ge=1, le=5)] | None = None
    config_bundle_id: str
    worker_lease: WorkerLease | None = None
    late_provider_result: dict | None = None
    prompt_version_id: str | None = None
    context_version_id: str | None = None
    risk_version_id: str | None = None
    execution_provenance: Literal["DETERMINISTIC", "REAL_PROVIDER", "NOT_SENT", "UNKNOWN"] = "DETERMINISTIC"
    parent_step_ids: list[str] = Field(default_factory=list)
    state: Literal["STARTED", "SUCCEEDED", "FAILED", "UNKNOWN_OUTCOME"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    duration_ms: int | None = None
    input_hash: str
    selected_evidence_ids: list[str] = Field(default_factory=list)
    input_snapshot: dict = Field(default_factory=dict)
    output: dict | None = None
    validation: dict = Field(default_factory=dict)
    model_id: str | None = None
    context_precount_tokens: int | None = None
    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    candidate_tokens: int | None = None
    thought_tokens: int | None = None
    tool_prompt_tokens: int | None = None
    total_tokens: int | None = None
    usage_status: Literal["PROVIDER_REPORTED", "NOT_APPLICABLE", "UNKNOWN"] = "NOT_APPLICABLE"
    provider_usage_raw: dict | None = None
    provider_request_id: str | None = None
    provider_service_tier: str | None = None
    rate_card_id: str | None = None
    estimated_usd_micros: int | None = None
    cost_status: Literal["ESTIMATED", "NOT_APPLICABLE", "UNKNOWN"] = "NOT_APPLICABLE"
    error_code: str | None = None


def retry_allowed(step):
    delay = step.get("retry_delay_seconds")
    return (step.get("leg_id") in {"A2", "A3", "A4", "A5"} and step.get("attempt_number", 1) == 1
        and step.get("state") == "FAILED" and step.get("execution_provenance") == "REAL_PROVIDER"
        and step.get("error_code") == "PROVIDER_HTTP_429" and step.get("output") is None and step.get("finished_at") is not None
        and type(delay) in {int, float} and 1 <= delay <= 5)


class UsageDaily(BaseModel):
    day_utc: str
    attempts_reserved: Annotated[StrictInt, Field(ge=0, le=120)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=120)] = 120
    live_enabled: bool = False
    credit_verified_until: AwareDatetime | None = None
    revision: Annotated[StrictInt, Field(ge=0)] = 0
    reserved_attempt_ids: list[str] = Field(default_factory=list, max_length=120)

class PolicyChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    expected_policy_revision: int = Field(ge=0)
    notes: str = Field(min_length=1, max_length=2000)


class PatternReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    status: Literal["ACTIVE", "RESOLVED"]
    notes: str = Field(min_length=1, max_length=2000)
