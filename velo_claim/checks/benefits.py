from __future__ import annotations

from datetime import date, datetime

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.models import KnowledgeStatus, normalize_code_system


def check_plan_benefits(state: dict, kg_client: Neo4jClientInterface) -> CheckResult:
    """Evaluate plan benefit knowledge without replacing member eligibility verification."""

    claim = state.get("canonical_claim", {})
    payer = claim.get("payer", {})
    service_date = claim.get("encounter", {}).get("service_date")
    coverage_start = (payer.get("coverage_period") or {}).get("start")
    employer_policy_id = payer.get("employer_policy_id") or payer.get("policy_id")
    issues: list[CheckIssue] = []
    results: list[dict] = []
    for line in claim.get("line_items", []):
        system = normalize_code_system(line.get("system"))
        if system != "CDT" or not line.get("code"):
            continue
        result = kg_client.query_plan_benefit(
            payer_id=payer.get("id", ""),
            plan_id=payer.get("plan_id", ""),
            procedure_code=line["code"],
            procedure_system=system,
            service_date=line.get("service_date") or service_date,
            employer_policy_id=employer_policy_id,
        )
        results.append(result.to_dict())
        evidence = {"kg_result": result.to_dict()}
        if result.status == KnowledgeStatus.UNAVAILABLE:
            issues.append(_issue("KG_BENEFIT_UNAVAILABLE", line["code"],
                                 "Neo4j is unavailable, so plan benefits could not be verified.", evidence))
        elif result.status == KnowledgeStatus.UNKNOWN and result.source == "NEO4J":
            issues.append(_issue("PLAN_BENEFIT_UNKNOWN", line["code"],
                                 "No matching plan benefit was found; this is not proof of exclusion or coverage.", evidence))
        elif result.status == KnowledgeStatus.NOT_SUPPORTED:
            issues.append(_issue("PLAN_BENEFIT_NOT_COVERED", line["code"],
                                 "The matched plan benefit explicitly marks this procedure as not covered.", evidence))
        elif result.status == KnowledgeStatus.CONDITIONAL:
            waiting_days = int((result.evidence.get("benefit") or {}).get("waiting_period_days") or 0)
            if not _waiting_period_satisfied(coverage_start, service_date, waiting_days):
                issues.append(_issue("PLAN_BENEFIT_WAITING_PERIOD", line["code"],
                                     f"The plan benefit has a {waiting_days}-day waiting period that is not proven satisfied.", evidence))
    return CheckResult("BENEFITS", "PASS" if not issues else "REVIEW", issues, {"kg_results": results})


def _issue(code: str, procedure_code: str, message: str, evidence: dict) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.ERROR,
        check_type="BENEFITS",
        field=f"canonical_claim.line_items.{procedure_code}",
        message=message,
        suggestion="Confirm the benefit with the payer or correct payer/plan identifiers before submission.",
        penalty=20,
        evidence=evidence,
    )


def _waiting_period_satisfied(start: str | None, service: str | None, required_days: int) -> bool:
    if required_days <= 0:
        return True
    start_date, service_date = _parse_date(start), _parse_date(service)
    return bool(start_date and service_date and (service_date - start_date).days >= required_days)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None
