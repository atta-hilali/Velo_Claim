from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.engine import bundled_codes_for_code, required_documents_for_code


def check_payer_rules(state: dict, payer_rules: PayerRuleSet, kg_client: Neo4jClientInterface) -> CheckResult:
    claim = state.get("canonical_claim", {})
    issues: list[CheckIssue] = []
    billed_codes = [line.get("code") for line in claim.get("line_items", []) if line.get("code")]

    if payer_rules.source in {"MOCK", "CACHED"}:
        issues.append(
            CheckIssue(
                code="PAYER_RULE_SOURCE_NOT_LIVE",
                severity=Severity.WARNING,
                check_type="PAYER_RULES",
                field="payer_rule_set.source",
                message=f"Payer rules came from {payer_rules.source}, not a live payer portal.",
                suggestion="Use live payer rules or confirm cached rule freshness before production submission.",
                penalty=5,
            )
        )

    for line in claim.get("line_items", []):
        code = line.get("code")
        if not code:
            continue
        for bundled in bundled_codes_for_code(cpt_code=code, payer_rules=payer_rules, kg_client=kg_client):
            if bundled in billed_codes:
                issues.append(
                    CheckIssue(
                        code="PAYER_RULE_BUNDLING_CONFLICT",
                        severity=Severity.ERROR,
                        check_type="PAYER_RULES",
                        field="canonical_claim.line_items",
                        message=f"CPT {bundled} appears bundled with {code} and should not be billed separately.",
                        suggestion="Remove the bundled line or route to coding review.",
                        penalty=20,
                    )
                )
        required_docs = required_documents_for_code(cpt_code=code, payer_rules=payer_rules, kg_client=kg_client)
        if required_docs:
            available = {
                str(doc.get("type") or doc.get("category") or doc.get("name", "")).upper()
                for doc in claim.get("attachments", [])
                if isinstance(doc, dict)
            }
            for doc in required_docs:
                if doc.upper() not in available:
                    issues.append(
                        CheckIssue(
                            code="PAYER_REQUIRED_DOCUMENT_MISSING",
                            severity=Severity.ERROR,
                            check_type="PAYER_RULES",
                            field="canonical_claim.attachments",
                            message=f"Payer rule requires document {doc} for CPT {code}.",
                            suggestion=f"Attach {doc} or remove CPT {code}.",
                            penalty=20,
                        )
                    )
    return CheckResult("PAYER_RULES", "PASS" if not issues else "REVIEW", issues, {"rule_source": payer_rules.source})
