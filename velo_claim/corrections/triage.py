from __future__ import annotations

from collections import OrderedDict
from typing import Any

from velo_claim.corrections.patches import is_protected_path, normalize_field_path


_EXTERNAL_CODES = {
    "ELIGIBILITY_DENIED",
    "ELIGIBILITY_NOT_CONFIRMED",
    "OUT_OF_NETWORK",
    "PA_DENIED",
    "PRIOR_AUTH_DENIED",
    "PRIOR_AUTH_REQUIRED",
    "PRIOR_AUTH_MISSING",
    "DUPLICATE_CLAIM",
    "DUPLICATE_SUBMISSION",
    "ROUTE_MISSING",
    "PAYER_MISMATCH",
}
_EXTERNAL_CHECKS = {
    "ELIGIBILITY",
    "PRIOR_AUTH",
    "PRIOR_AUTHORIZATION",
    "DUPLICATE",
    "SUBMISSION_READINESS",
}


def normalize_issue(issue: dict[str, Any], canonical_claim: dict[str, Any]) -> dict[str, Any]:
    original = str(issue.get("field_path") or issue.get("field") or "")
    field_path = normalize_field_path(original, canonical_claim)
    issue_id = str(issue.get("issue_id") or issue.get("id") or "")
    severity = str(issue.get("severity") or "WARNING")
    check_type = str(issue.get("check_type") or "UNKNOWN")
    code = str(issue.get("code") or "UNKNOWN")
    manual_reason = None
    if severity == "CRITICAL":
        manual_reason = "Critical findings require explicit reconciliation and cannot be auto-corrected."
    elif check_type in _EXTERNAL_CHECKS or code in _EXTERNAL_CODES:
        manual_reason = "The finding depends on an external payer, routing, or adjudication decision."
    elif field_path == "canonical_claim.attachments":
        manual_reason = "Supporting documents must be reconciled against an actual source artifact."
    elif not field_path.startswith("canonical_claim") or is_protected_path(field_path):
        manual_reason = "The finding targets protected or non-canonical authoritative data."
    return {
        "issue_id": issue_id,
        "code": code,
        "check_type": check_type,
        "severity": severity,
        "original_field": original,
        "field_path": field_path,
        "message": str(issue.get("message") or ""),
        "suggestion": str(issue.get("suggestion") or ""),
        "evidence": issue.get("evidence") or {},
        "manual_reason": manual_reason,
    }


def group_issues(
    issues: list[dict[str, Any]],
    canonical_claim: dict[str, Any],
    history_loader,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized = [normalize_issue(issue, canonical_claim) for issue in issues]
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for issue in normalized:
        grouped.setdefault(issue["field_path"], []).append(issue)
    groups = []
    for field_path, members in grouped.items():
        groups.append(
            {
                "field_path": field_path,
                "issues": members,
                "issue_ids": sorted({item["issue_id"] for item in members if item["issue_id"]}),
                "issue_codes": sorted({item["code"] for item in members}),
                "manual_reason": next((item["manual_reason"] for item in members if item["manual_reason"]), None),
                "history": history_loader(field_path),
            }
        )
    return normalized, groups
