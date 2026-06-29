from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult


def parse_payload(payload: str, payload_type: str) -> tuple[Any | None, CheckResult]:
    issues: list[CheckIssue] = []
    parsed = None
    try:
        if payload_type == "fhir_bundle_json":
            parsed = json.loads(payload)
        else:
            parsed = ET.fromstring(payload)
    except Exception as exc:
        issues.append(
            CheckIssue(
                code="PAYLOAD_PARSE_FAILED",
                severity=Severity.CRITICAL,
                check_type="PAYLOAD_CONFORMITY",
                field="claim_payload",
                message=f"Claim payload could not be parsed: {exc}",
                suggestion="Rebuild the payload using the persisted route decision.",
                penalty=100,
            )
        )
    return parsed, CheckResult("PAYLOAD_CONFORMITY", "PARSED" if parsed is not None else "FAILED", issues)


def check_payload_conformity(parsed_payload: Any, payload_type: str, route: dict) -> CheckResult:
    issues: list[CheckIssue] = []
    standard = route.get("claim_standard")
    if parsed_payload is None:
        return CheckResult("PAYLOAD_CONFORMITY", "FAILED", issues)

    if payload_type == "fhir_bundle_json":
        if parsed_payload.get("resourceType") != "Bundle":
            issues.append(_schema_issue("NPHIES payload must be a FHIR Bundle.", "claim_payload.resourceType"))
        if parsed_payload.get("type") != "message":
            issues.append(_schema_issue("NPHIES claim payload must be a message Bundle.", "claim_payload.type"))
    else:
        root_name = parsed_payload.tag
        if root_name != "Claim.Submission":
            issues.append(_schema_issue(f"{standard} XML must use Claim.Submission root.", "claim_payload.root"))
        if parsed_payload.find("Header") is None:
            issues.append(_schema_issue("XML claim payload is missing Header.", "claim_payload.Header"))
        if parsed_payload.find("Claim") is None:
            issues.append(_schema_issue("XML claim payload is missing Claim.", "claim_payload.Claim"))

    return CheckResult("PAYLOAD_CONFORMITY", "PASS" if not issues else "FAILED", issues)


def _schema_issue(message: str, field: str) -> CheckIssue:
    return CheckIssue(
        code="PAYLOAD_SCHEMA_MISMATCH",
        severity=Severity.ERROR,
        check_type="PAYLOAD_CONFORMITY",
        field=field,
        message=message,
        suggestion="Run the fallback rebuild path and re-parse the new payload.",
        penalty=20,
    )
