from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult


def check_metadata_consistency(state: dict) -> CheckResult:
    claim = state.get("canonical_claim", {})
    route = state.get("route", {})
    routing_context = state.get("routing_context", {})
    issues: list[CheckIssue] = []
    if not claim.get("claim_id"):
        issues.append(_issue("CLAIM_ID_MISSING", "canonical_claim.claim_id", "Claim ID is missing."))
    if not route.get("claim_standard"):
        issues.append(_issue("ROUTE_MISSING", "route.claim_standard", "Claim route is missing."))
    if claim.get("payer", {}).get("id") != routing_context.get("payer_id"):
        issues.append(
            _issue(
                "PAYER_MISMATCH",
                "canonical_claim.payer.id",
                "Canonical claim payer does not match routing context payer.",
            )
        )
    if not routing_context.get("facility_license"):
        issues.append(_issue("FACILITY_LICENSE_MISSING", "routing_context.facility_license", "Facility license is missing."))
    if not routing_context.get("provider_license"):
        issues.append(_issue("PROVIDER_LICENSE_MISSING", "routing_context.provider_license", "Provider license is missing."))
    return CheckResult("METADATA", "PASS" if not issues else "FAILED", issues)


def _issue(code: str, field: str, message: str) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.ERROR,
        check_type="METADATA",
        field=field,
        message=message,
        suggestion="Fix source/routing context before submission.",
        penalty=20,
    )
