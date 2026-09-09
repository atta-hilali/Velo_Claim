from __future__ import annotations

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.engine import bundled_codes_for_code


def check_payer_rules(state: dict, payer_rules: PayerRuleSet, kg_client: Neo4jClientInterface) -> CheckResult:
    claim = state.get("canonical_claim", {})
    payer = claim.get("payer", {})
    issues: list[CheckIssue] = []
    kg_results: list[dict] = []
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
        bundled_codes, kg_result = bundled_codes_for_code(
            procedure_code=code,
            procedure_system=line.get("system") or "CPT",
            payer_id=payer.get("id"),
            plan_id=payer.get("plan_id"),
            service_date=line.get("service_date") or claim.get("encounter", {}).get("service_date"),
            payer_rules=payer_rules,
            kg_client=kg_client,
        )
        kg_results.append(kg_result.to_dict())
        for bundled in bundled_codes:
            if bundled in billed_codes:
                issues.append(
                    CheckIssue(
                        code="PAYER_RULE_BUNDLING_CONFLICT",
                        severity=Severity.ERROR,
                        check_type="PAYER_RULES",
                        field="canonical_claim.line_items",
                        message=f"Procedure {bundled} appears bundled with {code} and should not be billed separately.",
                        suggestion="Remove the bundled line or route to coding review.",
                        penalty=20,
                    )
                )
    return CheckResult(
        "PAYER_RULES",
        "PASS" if not issues else "REVIEW",
        issues,
        {"rule_source": payer_rules.source, "kg_bundling_results": kg_results},
    )
