from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult


def check_financial_consistency(state: dict) -> CheckResult:
    claim = state.get("canonical_claim", {})
    amount = claim.get("amount", {})
    lines = claim.get("line_items", [])
    issues: list[CheckIssue] = []
    for index, line in enumerate(lines):
        missing_fields = line.get("missing_financial_fields") or []
        if missing_fields:
            issues.append(
                _issue(
                    "FINANCIAL_FIELDS_MISSING",
                    f"canonical_claim.line_items[{index}].missing_financial_fields",
                    f"Line item {line.get('id') or index + 1} is missing financial fields: {', '.join(missing_fields)}.",
                    severity=Severity.ERROR,
                    penalty=20,
                )
            )
    line_gross = round(sum(_money(line.get("gross", 0.0)) for line in lines), 2)
    line_net = round(sum(_money(line.get("net", 0.0)) for line in lines), 2)
    line_patient_share = round(sum(_money(line.get("patient_share", 0.0)) for line in lines), 2)
    if round(_money(amount.get("gross", 0.0)), 2) != line_gross:
        issues.append(_issue("FINANCIAL_GROSS_MISMATCH", "canonical_claim.amount.gross", "Claim gross does not equal line gross total."))
    if round(_money(amount.get("net", 0.0)), 2) != line_net:
        issues.append(_issue("FINANCIAL_NET_MISMATCH", "canonical_claim.amount.net", "Claim net does not equal line net total."))
    if round(_money(amount.get("patient_share", 0.0)), 2) != line_patient_share:
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


def _issue(
    code: str,
    field: str,
    message: str,
    *,
    severity: Severity = Severity.ERROR,
    penalty: int = 20,
) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=severity,
        check_type="FINANCIAL",
        field=field,
        message=message,
        suggestion="Recalculate claim totals from line items before submission.",
        penalty=penalty,
    )


def _money(value: object) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
