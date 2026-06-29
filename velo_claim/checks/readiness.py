from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult


def check_submission_readiness(state: dict, prior_auth_result: CheckResult) -> CheckResult:
    issues: list[CheckIssue] = []
    if state.get("payload_status") not in {"DRAFT_BUILT", "VALIDATED", "READY_TO_SUBMIT"}:
        issues.append(
            CheckIssue(
                code="PAYLOAD_NOT_READY",
                severity=Severity.ERROR,
                check_type="READINESS",
                field="payload_status",
                message="Payload is not in a submittable status.",
                suggestion="Rebuild or validate the payload before submission.",
                penalty=20,
            )
        )
    if any(issue.code == "PA_REQUIRED_MISSING" for issue in prior_auth_result.issues):
        issues.append(
            CheckIssue(
                code="SUBMISSION_BLOCKED_BY_PA",
                severity=Severity.ERROR,
                check_type="READINESS",
                field="prior_auth",
                message="Submission is blocked until prior authorization is approved.",
                suggestion="Use the generated PA payload and resume validation when the payer response arrives.",
                penalty=20,
            )
        )
    return CheckResult("READINESS", "PASS" if not issues else "BLOCKED", issues)
