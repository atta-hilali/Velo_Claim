from __future__ import annotations

from datetime import date, datetime

from velo_claim.core.enums import EligibilityStatus, Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.storage.interfaces import CacheStoreInterface


def run_eligibility_check(state: dict, payer_rules: PayerRuleSet, cache: CacheStoreInterface) -> CheckResult:
    claim = state.get("canonical_claim", {})
    payer = claim.get("payer", {})
    service_date = claim.get("encounter", {}).get("service_date")
    cache_key = f"eligibility:{claim.get('patient', {}).get('id')}:{payer.get('id')}:{service_date}"
    cached = cache.get(cache_key)
    if cached:
        return CheckResult("ELIGIBILITY", EligibilityStatus.CACHED_VALID, data={"cached": cached})

    issues: list[CheckIssue] = []
    status = str(payer.get("coverage_status") or "").lower()
    if status != "active":
        issues.append(
            CheckIssue(
                code="ELIGIBILITY_INACTIVE",
                severity=Severity.CRITICAL,
                check_type="ELIGIBILITY",
                field="canonical_claim.payer.coverage_status",
                message="Coverage is not active.",
                suggestion="Confirm active coverage or route the claim to manual review.",
                penalty=100,
            )
        )
    period = payer.get("coverage_period") or {}
    if period and not _date_in_period(service_date, period):
        issues.append(
            CheckIssue(
                code="ELIGIBILITY_OUTSIDE_PERIOD",
                severity=Severity.CRITICAL,
                check_type="ELIGIBILITY",
                field="canonical_claim.encounter.service_date",
                message="Service date is outside the coverage period.",
                suggestion="Find another active coverage or correct the service date.",
                penalty=100,
            )
        )
    if state.get("route", {}).get("eligibility_profile") == "DAMAN_VOI":
        voi = state.get("source_context", {}).get("coverage", {}).get("voi_verified")
        if voi is not True:
            issues.append(
                CheckIssue(
                    code="DAMAN_VOI_MISSING",
                    severity=Severity.ERROR,
                    check_type="ELIGIBILITY",
                    field="source_context.coverage.voi_verified",
                    message="DAMAN VOI flag is missing or false.",
                    suggestion="Run DAMAN verification of insurance before submission.",
                    penalty=20,
                )
            )
    result = CheckResult("ELIGIBILITY", EligibilityStatus.PASS if not issues else EligibilityStatus.FAIL_HOLD_CRITICAL, issues)
    if result.passes:
        cache.set(cache_key, result.to_dict(), ttl_seconds=payer_rules.eligibility_ttl_seconds)
    return result


def _date_in_period(service_date: str | None, period: dict) -> bool:
    current = _parse_date(service_date)
    start = _parse_date(period.get("start"))
    end = _parse_date(period.get("end"))
    if not current:
        return True
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value).split("T")[0])
        except ValueError:
            return None
