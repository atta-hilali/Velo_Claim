from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from velo_claim.core.enums import (
    ClaimStandard,
    EligibilityStatus,
    Jurisdiction,
    PayloadStatus,
    PriorAuthStatus,
    Severity,
    ValidationStatus,
)
from velo_claim.core.utils import utc_now


@dataclass(slots=True)
class ClaimError:
    code: str
    severity: Severity
    check_type: str
    field: str
    message: str
    suggestion: str
    agent: str
    node: str
    ts: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SourceContext:
    patient: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    encounter: dict[str, Any] = field(default_factory=dict)
    provider: dict[str, Any] = field(default_factory=dict)
    facility: dict[str, Any] = field(default_factory=dict)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    procedures: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    charge_items: list[dict[str, Any]] = field(default_factory=list)
    payer_rules: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RoutingContext:
    payer_id: str = "UNKNOWN"
    payer_name: str = "UNKNOWN"
    plan_id: str = "UNKNOWN"
    jurisdiction_hint: str | None = None
    facility_license_system: str | None = None
    facility_license: str | None = None
    provider_license_system: str | None = None
    provider_license: str | None = None
    currency: str = "AED"
    patient_identifier_types: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Route:
    jurisdiction: Jurisdiction
    claim_standard: ClaimStandard
    prior_auth_standard: ClaimStandard
    eligibility_profile: str
    payer_rule_profile: str
    submission_channel: str
    confidence: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CallbackState:
    bg_job_id: str | None = None
    thread_id: str | None = None
    checkpoint_id: str | None = None
    poll_attempt: int = 0
    next_poll_at: str | None = None
    waiting_since: str | None = None
    resume_node: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CheckIssue:
    code: str
    severity: Severity
    check_type: str
    field: str
    message: str
    suggestion: str
    penalty: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_error(self, agent: str, node: str) -> ClaimError:
        return ClaimError(
            code=self.code,
            severity=self.severity,
            check_type=self.check_type,
            field=self.field,
            message=self.message,
            suggestion=self.suggestion,
            agent=agent,
            node=node,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CheckResult:
    check_type: str
    status: str
    issues: list[CheckIssue] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def passes(self) -> bool:
        return not any(issue.severity in {Severity.CRITICAL, Severity.ERROR} for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_type": self.check_type,
            "status": self.status,
            "passes": self.passes,
            "issues": [issue.to_dict() for issue in self.issues],
            "data": self.data,
        }


@dataclass(slots=True)
class PayerRuleSet:
    payer_id: str
    plan_id: str
    eligibility_ttl_seconds: int
    pa_required_cpt_codes: list[str]
    bundling_rules: dict[str, list[str]]
    required_doc_types: dict[str, list[str]]
    submission_channel: str
    source: Literal["LIVE", "CACHED", "MOCK"] = "MOCK"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ValidationReport:
    claim_id: str
    score: int
    status: ValidationStatus
    issues: list[CheckIssue]
    checks: list[CheckResult]
    generated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "score": self.score,
            "status": self.status,
            "summary": {
                "total_issues": len(self.issues),
                "critical": len([i for i in self.issues if i.severity == Severity.CRITICAL]),
                "errors": len([i for i in self.issues if i.severity == Severity.ERROR]),
                "warnings": len([i for i in self.issues if i.severity == Severity.WARNING]),
            },
            "checks": [check.to_dict() for check in self.checks],
            "issues": [issue.to_dict() for issue in self.issues],
            "generated_at": self.generated_at,
        }


CanonicalState = dict[str, Any]


def append_error(state: CanonicalState, error: ClaimError) -> CanonicalState:
    return {**state, "errors": [*state.get("errors", []), error.to_dict()]}


def append_warning(state: CanonicalState, warning: dict[str, Any]) -> CanonicalState:
    return {**state, "warnings": [*state.get("warnings", []), warning]}


def default_state() -> CanonicalState:
    return {
        "canonical_claim": {},
        "claim": {},
        "claim_format": None,
        "jurisdiction": None,
        "claim_payload": None,
        "claim_payload_type": None,
        "payload_status": None,
        "payload_version": 0,
        "rebuild_attempt_count": 0,
        "pa_payload": None,
        "pa_payload_type": None,
        "pa_payload_version": 0,
        "route": {},
        "source_context": {},
        "routing_context": {},
        "callback_state": CallbackState().to_dict(),
        "next_agent": None,
        "errors": [],
        "warnings": [],
    }


def payload_type_for_standard(standard: ClaimStandard) -> str:
    if standard == ClaimStandard.NPHIES:
        return "fhir_bundle_json"
    return "application/xml"


def payload_extension(payload_type: str) -> str:
    return "json" if payload_type == "fhir_bundle_json" else "xml"
