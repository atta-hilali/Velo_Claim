from __future__ import annotations

from datetime import date, datetime

from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.core.enums import PriorAuthStatus, Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.engine import pa_required_for_code
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
    required_codes = [
        line.get("code")
        for line in claim.get("line_items", [])
        if pa_required_for_code(
            payer_id=payer.get("id", ""),
            plan_id=payer.get("plan_id", ""),
            cpt_code=line.get("code", ""),
            payer_rules=payer_rules,
            kg_client=kg_client,
        )
    ]
    if not required_codes:
        return state, CheckResult("PRIOR_AUTH", PriorAuthStatus.NOT_REQUIRED, data={"required_codes": []})

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
        request_id = repository.insert_prior_auth_request(
            claim["claim_id"],
            {
                "standard": state.get("route", {}).get("prior_auth_standard"),
                "object_uri": state.get("pa_payload_uri"),
                "status": PriorAuthStatus.REQUIRED_MISSING,
                "required_codes": missing_codes,
            },
        )
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
        try:
            return date.fromisoformat(str(value).split("T")[0])
        except ValueError:
            return None
