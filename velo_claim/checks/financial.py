from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult


def check_financial_consistency(state: dict) -> CheckResult:
    claim = state.get("canonical_claim", {})
    amount = claim.get("amount", {})
    lines = claim.get("line_items", [])
    issues: list[CheckIssue] = []
    line_gross = round(sum(float(line.get("gross", 0.0)) for line in lines), 2)
    line_net = round(sum(float(line.get("net", 0.0)) for line in lines), 2)
    line_patient_share = round(sum(float(line.get("patient_share", 0.0)) for line in lines), 2)
    if round(float(amount.get("gross", 0.0)), 2) != line_gross:
        issues.append(_issue("FINANCIAL_GROSS_MISMATCH", "canonical_claim.amount.gross", "Claim gross does not equal line gross total."))
    if round(float(amount.get("net", 0.0)), 2) != line_net:
        issues.append(_issue("FINANCIAL_NET_MISMATCH", "canonical_claim.amount.net", "Claim net does not equal line net total."))
    if round(float(amount.get("patient_share", 0.0)), 2) != line_patient_share:
        issues.append(
            _issue(
                "FINANCIAL_PATIENT_SHARE_MISMATCH",
                "canonical_claim.amount.patient_share",
                "Claim patient share does not equal line patient share total.",
            )
        )
    expected_currency = state.get("routing_context", {}).get("currency")
    if expected_currency and amount.get("currency") != expected_currency:
        issues.append(_issue("CURRENCY_MISMATCH", "canonical_claim.amount.currency", "Claim currency does not match route currency."))
    return CheckResult("FINANCIAL", "PASS" if not issues else "FAILED", issues)


def _issue(code: str, field: str, message: str) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.ERROR,
        check_type="FINANCIAL",
        field=field,
        message=message,
        suggestion="Recalculate claim totals from line items before submission.",
        penalty=20,
    )
