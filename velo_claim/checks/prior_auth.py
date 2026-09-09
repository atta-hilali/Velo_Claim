from __future__ import annotations

from datetime import date, datetime

from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.core.enums import PriorAuthStatus, Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.models import KnowledgeStatus, normalize_code_system
from velo_claim.rules.engine import prior_auth_requirement_for_code
from velo_claim.storage.interfaces import RepositoryInterface


def run_prior_auth_check(
    *,
    state: dict,
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
    repository: RepositoryInterface,
    pa_builder: PAClaimBuilderModule,
) -> tuple[dict, CheckResult]:
    claim = state.get("canonical_claim", {})
    payer = claim.get("payer", {})
    decisions = [
        (
            line,
            prior_auth_requirement_for_code(
                payer_id=payer.get("id", ""),
                plan_id=payer.get("plan_id", ""),
                procedure_code=line.get("code", ""),
                procedure_system=normalize_code_system(line.get("system")),
                service_date=line.get("service_date") or claim.get("encounter", {}).get("service_date"),
                payer_rules=payer_rules,
                kg_client=kg_client,
            ),
        )
        for line in claim.get("line_items", [])
        if line.get("code")
    ]
    knowledge_issues = [
        CheckIssue(
            code=f"PA_KNOWLEDGE_{decision.status}",
            severity=Severity.ERROR,
            check_type="PRIOR_AUTH",
            field=f"canonical_claim.line_items.{line.get('code')}",
            message=decision.reason or f"Prior-authorization requirement is {decision.status}.",
            suggestion="Confirm PA requirements with the payer before submission.",
            penalty=20,
            evidence={"kg_result": decision.to_dict()},
        )
        for line, decision in decisions
        if normalize_code_system(line.get("system")) == "CDT"
        and decision.source in {"NEO4J", "RULE_ENGINE"}
        and decision.status in {KnowledgeStatus.UNKNOWN, KnowledgeStatus.UNAVAILABLE, KnowledgeStatus.CONFLICT}
    ]
    if knowledge_issues:
        return state, CheckResult(
            "PRIOR_AUTH",
            "REVIEW_REQUIRED",
            knowledge_issues,
            {"knowledge_results": [decision.to_dict() for _, decision in decisions]},
        )
    required_codes = [line.get("code") for line, decision in decisions if decision.status == KnowledgeStatus.REQUIRED]
    if not required_codes:
        return state, CheckResult(
            "PRIOR_AUTH",
            PriorAuthStatus.NOT_REQUIRED,
            data={"required_codes": [], "knowledge_results": [decision.to_dict() for _, decision in decisions]},
        )

    issues: list[CheckIssue] = []
    valid_refs: list[str] = []
    missing_codes: list[str] = []
    for code in required_codes:
        response = repository.find_prior_auth_response(claim["claim_id"], payer.get("id"), code)
        if response and auth_valid(response, code, claim.get("encounter", {}).get("service_date")):
            valid_refs.append(response.get("pre_auth_ref"))
        elif response:
            issues.append(
                CheckIssue(
                    code="PA_EXPIRED_OR_DENIED",
                    severity=Severity.CRITICAL,
                    check_type="PRIOR_AUTH",
                    field="prior_auth_response",
                    message=f"Prior authorization for {code} exists but is expired, denied, or outside service date.",
                    suggestion="Obtain a fresh authorization before submission.",
                    penalty=100,
                )
            )
        else:
            missing_codes.append(code)

    if missing_codes and not issues:
        state = pa_builder.build(state, missing_codes)
        request_id = state.get("pa_request_id")
        if not request_id:
            raise ValueError("PA builder did not return a persisted pa_request_id.")
        issues.append(
            CheckIssue(
                code="PA_REQUIRED_MISSING",
                severity=Severity.ERROR,
                check_type="PRIOR_AUTH",
                field="canonical_claim.procedures",
                message=f"Prior authorization is required for {', '.join(missing_codes)}.",
                suggestion="Submit the generated PA payload and wait for approval.",
                penalty=20,
                evidence={"request_id": request_id, "pa_payload_uri": state.get("pa_payload_uri")},
            )
        )
    if valid_refs:
        state = {
            **state,
            "canonical_claim": {
                **claim,
                "pre_auth_ref": valid_refs[0],
            },
        }
    status = PriorAuthStatus.ALREADY_VALID if valid_refs and not issues else PriorAuthStatus.REQUIRED_MISSING
    return state, CheckResult("PRIOR_AUTH", status, issues, {"required_codes": required_codes, "valid_refs": valid_refs})


def auth_valid(response: dict, code: str, service_date: str | None) -> bool:
    status = str(response.get("status", "")).lower()
    if status not in {"approved", "active"}:
        return False
    if code not in response.get("cpt_codes", []):
        return False
    return _date_in_period(service_date, {"start": response.get("valid_from"), "end": response.get("valid_to")})


def _date_in_period(value: str | None, period: dict) -> bool:
    current = _parse_date(value)
    start = _parse_date(period.get("start"))
    end = _parse_date(period.get("end"))
    if not current:
        return True
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
            try:
                return datetime.strptime(str(value), pattern).date()
            except ValueError:
                continue
        return None
