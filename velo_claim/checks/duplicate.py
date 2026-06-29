from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult
from velo_claim.core.utils import sha256_obj
from velo_claim.storage.interfaces import RepositoryInterface


def check_duplicate(state: dict, repository: RepositoryInterface) -> CheckResult:
    claim = state.get("canonical_claim", {})
    fingerprint = sha256_obj(
        {
            "payer_id": claim.get("payer", {}).get("id"),
            "service_date": claim.get("encounter", {}).get("service_date"),
            "codes": sorted(line.get("code") for line in claim.get("line_items", [])),
        }
    )
    duplicate = repository.find_duplicate_submission(claim.get("claim_id"), claim.get("payer", {}).get("id"), fingerprint)
    issues = []
    if duplicate:
        issues.append(
            CheckIssue(
                code="DUPLICATE_CLAIM",
                severity=Severity.CRITICAL,
                check_type="DUPLICATE",
                field="canonical_claim",
                message="A similar claim has already been submitted.",
                suggestion="Review the existing submission before continuing.",
                penalty=100,
                evidence={"duplicate": duplicate},
            )
        )
    return CheckResult("DUPLICATE", "PASS" if not issues else "FAILED", issues, {"fingerprint": fingerprint})
