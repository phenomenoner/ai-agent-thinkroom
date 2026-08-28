from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


BoundedText = Annotated[str, Field(min_length=1, max_length=4000)]
BoundedItem = Annotated[str, Field(min_length=1, max_length=4000)]
BoundedId = Annotated[str, Field(min_length=1, max_length=128)]


def text(min_len: int = 1, max_len: int = 4000) -> Any:
    return Field(min_length=min_len, max_length=max_len)


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NOT_APPLICABLE = "not_applicable"


class Disposition(StrEnum):
    RECOMMEND = "RECOMMEND"
    COMBINE = "COMBINE"
    REJECT_ALL = "REJECT_ALL"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"


class JobState(StrEnum):
    QUEUED = "queued"
    FRAMING = "framing"
    ROLLING_OUT = "rolling_out"
    CRITIQUING = "critiquing"
    SYNTHESIZING = "synthesizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvidenceV1(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    statement: BoundedText
    relationship: Literal["supports", "contradicts"]
    source_label: str | None = Field(default=None, min_length=1, max_length=4000)
    source_reference: str | None = Field(default=None, min_length=1, max_length=2048)
    verification_status: EvidenceStatus
    verification_basis: str | None = Field(default=None, min_length=1, max_length=4000)
    verification_warning: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def verified_has_basis(self) -> EvidenceV1:
        if self.verification_status is EvidenceStatus.VERIFIED and not (
            self.verification_basis and self.source_reference
        ):
            raise ValueError("verified evidence requires verification basis and exact reference")
        return self


class ClaimV1(StrictModel):
    schema_version: Literal[1] = 1
    statement: BoundedText
    evidence_ids: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]] = Field(
        min_length=1, max_length=50
    )


class PerspectiveV1(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: BoundedText
    hypothesis: BoundedText
    approach: BoundedText
    differentiator: BoundedText


class FrameOutputV1(StrictModel):
    schema_version: Literal[1] = 1
    decision: BoundedText
    scope: BoundedText
    constraints: list[BoundedItem] = Field(min_length=1, max_length=50)
    success_criteria: list[BoundedItem] = Field(min_length=1, max_length=50)
    ambiguities: list[BoundedItem] = Field(min_length=1, max_length=50)
    research_questions: list[BoundedItem] = Field(min_length=1, max_length=50)


class ForkOutputV1(StrictModel):
    schema_version: Literal[1] = 1
    perspectives: list[PerspectiveV1] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def perspective_ids_are_unique(self) -> ForkOutputV1:
        ids = [perspective.id for perspective in self.perspectives]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate perspective IDs")
        return self


class BranchOutputV1(StrictModel):
    schema_version: Literal[1] = 1
    summary: str = Field(min_length=1, max_length=12000)
    claims: list[ClaimV1] = Field(min_length=1, max_length=50)
    supporting_evidence: list[EvidenceV1] = Field(max_length=50)
    contradicting_evidence: list[EvidenceV1] = Field(max_length=50)
    assumptions: list[BoundedItem] = Field(min_length=1, max_length=50)
    uncertainties: list[BoundedItem] = Field(min_length=1, max_length=50)
    falsifiers: list[BoundedItem] = Field(min_length=1, max_length=50)
    next_checks: list[BoundedItem] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def evidence_refs_resolve(self) -> BranchOutputV1:
        if any(item.relationship != "supports" for item in self.supporting_evidence):
            raise ValueError("supporting evidence must support")
        if any(item.relationship != "contradicts" for item in self.contradicting_evidence):
            raise ValueError("contradicting evidence must contradict")
        evidence = self.supporting_evidence + self.contradicting_evidence
        ids = [e.id for e in evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence IDs")
        available = set(ids)
        for claim in self.claims:
            if not set(claim.evidence_ids) <= available:
                raise ValueError("claim references absent evidence")
        return self


class BranchAssessmentV1(StrictModel):
    branch_id: BoundedId
    strengths: list[BoundedItem] = Field(min_length=1, max_length=50)
    weaknesses: list[BoundedItem] = Field(min_length=1, max_length=50)
    support_level: Literal["strong", "mixed", "weak"]


class CritiqueOutputV1(StrictModel):
    schema_version: Literal[1] = 1
    agreements: list[BoundedItem] = Field(min_length=1, max_length=50)
    contradictions: list[BoundedItem] = Field(min_length=1, max_length=50)
    unsupported_claims: list[BoundedItem] = Field(min_length=1, max_length=50)
    blind_spots: list[BoundedItem] = Field(min_length=1, max_length=50)
    discriminating_evidence: list[BoundedItem] = Field(min_length=1, max_length=50)
    branch_assessments: list[BranchAssessmentV1] = Field(min_length=1, max_length=6)
    consumed_branch_ids: list[BoundedId] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def unique_branch_references(self) -> CritiqueOutputV1:
        assessment_ids = [item.branch_id for item in self.branch_assessments]
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("duplicate branch assessment")
        if len(self.consumed_branch_ids) != len(set(self.consumed_branch_ids)):
            raise ValueError("duplicate consumed branch ID")
        return self


class AlternativeV1(StrictModel):
    title: BoundedText
    description: BoundedText
    tradeoffs: list[BoundedItem] = Field(min_length=1, max_length=50)


class EvidenceLedgerItemV1(StrictModel):
    evidence_id: BoundedId
    branch_id: BoundedId
    statement: BoundedText
    verification_status: EvidenceStatus
    provenance: BoundedText


MAX_SYNTHESIS_EVIDENCE_ITEMS = 50


class SynthesisOutputV1(StrictModel):
    schema_version: Literal[1] = 1
    disposition: Disposition
    recommendation: str = Field(min_length=1, max_length=12000)
    rationale: str = Field(min_length=1, max_length=12000)
    ranked_alternatives: list[AlternativeV1] = Field(min_length=1, max_length=50)
    evidence_ledger: list[EvidenceLedgerItemV1] = Field(
        min_length=1, max_length=MAX_SYNTHESIS_EVIDENCE_ITEMS
    )
    disagreements: list[BoundedItem] = Field(min_length=1, max_length=50)
    uncertainties: list[BoundedItem] = Field(min_length=1, max_length=50)
    falsifiers: list[BoundedItem] = Field(min_length=1, max_length=50)
    next_actions: list[BoundedItem] = Field(min_length=1, max_length=20)
    source_attempt_id: BoundedId
    consumed_branch_ids: list[BoundedId] = Field(min_length=1, max_length=6)
    consumed_critique_id: BoundedId


class BranchInputV1(StrictModel):
    branch_id: BoundedId
    output: BranchOutputV1


class FailedBranchV1(StrictModel):
    branch_id: BoundedId
    error: BoundedText


# Strict, phase-specific provider inputs. These models are validated before the backend sees data.
class FrameInputV1(StrictModel):
    question: str = Field(min_length=10, max_length=10000)
    context: str = Field(default="", max_length=100000)
    domain: Literal["generic", "coding", "trading"]
    guidance: BoundedText
    safety: BoundedText
    strategy: str = Field(default="orthogonal", min_length=1, max_length=128)
    validation_feedback: str | None = Field(default=None, min_length=1, max_length=4000)


class ForkInputV1(StrictModel):
    frame: FrameOutputV1
    branch_count: int = Field(ge=2, le=6)
    domain: Literal["generic", "coding", "trading"]
    strategy: str = Field(min_length=1, max_length=128)
    fallbacks: list[PerspectiveV1] = Field(min_length=2, max_length=6)
    validation_feedback: str | None = Field(default=None, min_length=1, max_length=4000)


class RolloutInputV1(StrictModel):
    frame: FrameOutputV1
    perspective: PerspectiveV1
    context: str = Field(default="", max_length=100000)
    domain_guidance: BoundedText
    safety: BoundedText
    validation_feedback: str | None = Field(default=None, min_length=1, max_length=4000)


class CritiqueInputV1(StrictModel):
    successful_branches: list[BranchInputV1] = Field(min_length=1, max_length=6)
    successful_branch_ids: list[BoundedId] = Field(min_length=1, max_length=6)
    failed_branches: list[FailedBranchV1] = Field(max_length=6)
    validation_feedback: str | None = Field(default=None, min_length=1, max_length=4000)


class SynthesisInputV1(StrictModel):
    frame: FrameOutputV1
    successful_branches: list[BranchInputV1] = Field(min_length=1, max_length=6)
    failed_branches: list[FailedBranchV1] = Field(max_length=6)
    critique: CritiqueOutputV1
    critique_id: BoundedId
    successful_branch_ids: list[BoundedId] = Field(min_length=1, max_length=6)
    validation_feedback: str | None = Field(default=None, min_length=1, max_length=4000)


Phase = Literal["frame", "fork", "rollout", "critique", "synthesis"]
PhaseInput = FrameInputV1 | ForkInputV1 | RolloutInputV1 | CritiqueInputV1 | SynthesisInputV1
_PHASE_INPUTS: dict[str, type[StrictModel]] = {
    "frame": FrameInputV1,
    "fork": ForkInputV1,
    "rollout": RolloutInputV1,
    "critique": CritiqueInputV1,
    "synthesis": SynthesisInputV1,
}


class BackendRequestV1(StrictModel):
    schema_version: Literal[1] = 1
    phase: Phase
    job_id: str
    attempt_id: str
    branch_id: str | None = None
    prompt_version: BoundedId
    input: PhaseInput
    expected_output_schema: BoundedId
    deadline: datetime
    correlation_id: str

    @model_validator(mode="before")
    @classmethod
    def validate_phase_input(cls, values: Any) -> Any:
        if isinstance(values, dict) and isinstance(values.get("phase"), str):
            phase = values["phase"]
            model = _PHASE_INPUTS.get(phase)
            if model is not None:
                values = dict(values)
                values["input"] = model.model_validate(values.get("input", {}))
        return values


class ResearchRequest(StrictModel):
    question: str = Field(min_length=10, max_length=10000)
    context: str | None = Field(default=None, max_length=100000)
    domain: Literal["generic", "coding", "trading"] = "generic"
    strategy: str = Field(default="orthogonal", min_length=1, max_length=128)
    branch_count: int = Field(default=3, ge=2, le=6)
    deadline_seconds: int | None = Field(default=None, ge=30, le=7200)
    deadline: datetime | None = None


class ErrorBody(StrictModel):
    code: str
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class JobResource(StrictModel):
    job_id: str
    state: JobState
    created_at: datetime
    url: str


class JobList(StrictModel):
    items: list[JobResource]
    next_cursor: str | None = None


class TerminalErrorV1(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ResearchBranchV1(StrictModel):
    branch_id: BoundedId
    state: Literal["succeeded", "failed"]
    output: BranchOutputV1 | None = None
    error: str | None = Field(default=None, max_length=4000)


class TransitionRecordV1(StrictModel):
    id: int = Field(ge=1)
    job_id: BoundedId
    attempt_id: BoundedId | None = None
    from_state: JobState | None = None
    to_state: JobState
    at: datetime
    reason: str | None = Field(default=None, max_length=4000)
    correlation_id: str | None = Field(default=None, max_length=128)


class AttemptRecordV1(StrictModel):
    attempt_id: BoundedId
    job_id: BoundedId
    number: int = Field(ge=1)
    state: Literal["active", "terminal", "abandoned"]
    started_at: datetime
    ended_at: datetime | None = None
    outcome: str | None = Field(default=None, max_length=128)
    recovery_reason: str | None = Field(default=None, max_length=4000)
    backend: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)


class ResearchDetail(StrictModel):
    job_id: str
    state: JobState
    request: ResearchRequest
    created_at: datetime
    updated_at: datetime
    attempt_id: str | None = None
    attempt_number: int | None = None
    branches: list[ResearchBranchV1] = Field(default_factory=list)
    perspectives: list[PerspectiveV1] = Field(default_factory=list)
    frame: FrameOutputV1 | None = None
    critique: CritiqueOutputV1 | None = None
    critique_id: BoundedId | None = None
    synthesis: SynthesisOutputV1 | None = None
    terminal_error: TerminalErrorV1 | None = None
    attempts: list[AttemptRecordV1] = Field(default_factory=list)
    transitions: list[TransitionRecordV1] = Field(default_factory=list)
