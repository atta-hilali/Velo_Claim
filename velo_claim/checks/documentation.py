from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.engine import required_documents_for_code


def check_documentation(state: dict, payer_rules: PayerRuleSet, kg_client: Neo4jClientInterface) -> CheckResult:
    claim = state.get("canonical_claim", {})
    attachments = claim.get("attachments", [])
    available = {str(doc.get("type") or doc.get("category") or doc.get("name", "")).upper() for doc in attachments if isinstance(doc, dict)}
    issues: list[CheckIssue] = []
    if not claim.get("encounter", {}).get("id"):
        issues.append(_issue("ENCOUNTER_MISSING", "canonical_claim.encounter.id", "Encounter reference is missing."))
    if not attachments:
        issues.append(
            CheckIssue(
                code="DOCUMENTATION_WARNING",
                severity=Severity.WARNING,
                check_type="DOCUMENTATION",
                field="canonical_claim.attachments",
                message="No supporting attachments are present.",
                suggestion="Attach SOAP note or clinical document before payer submission where required.",
                penalty=5,
            )
        )
    for line in claim.get("line_items", []):
        for required in required_documents_for_code(cpt_code=line.get("code"), payer_rules=payer_rules, kg_client=kg_client):
            if required.upper() not in available:
                issues.append(_issue("REQUIRED_DOCUMENT_MISSING", "canonical_claim.attachments", f"Required document is missing: {required}."))
    return CheckResult("DOCUMENTATION", "PASS" if not issues else "REVIEW", issues)


def _issue(code: str, field: str, message: str) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.ERROR,
        check_type="DOCUMENTATION",
        field=field,
        message=message,
        suggestion="Attach or extract the required documentation.",
        penalty=20,
    )
