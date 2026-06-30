from __future__ import annotations

from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.checks.coding import check_coding_consistency
from velo_claim.checks.documentation import check_documentation
from velo_claim.checks.duplicate import check_duplicate
from velo_claim.checks.eligibility_graph import run_eligibility_subgraph
from velo_claim.checks.financial import check_financial_consistency
from velo_claim.checks.metadata import check_metadata_consistency
from velo_claim.checks.payload import check_payload_conformity
from velo_claim.checks.payer_rules import check_payer_rules
from velo_claim.checks.prior_auth_graph import run_prior_auth_subgraph
from velo_claim.checks.readiness import check_submission_readiness
from velo_claim.core.enums import EligibilityStatus, PriorAuthStatus, Severity, ValidationStatus
from velo_claim.core.models import CheckResult, PayerRuleSet, ValidationReport
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.storage.interfaces import CacheStoreInterface, ObjectStoreInterface, RepositoryInterface


def run_validation_checks(
    *,
    state: dict,
    parsed_payload: object,
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
    repository: RepositoryInterface,
    object_store: ObjectStoreInterface,
    cache: CacheStoreInterface,
    pa_builder: PAClaimBuilderModule,
) -> tuple[dict, list[CheckResult]]:
    checks: list[CheckResult] = []
    checks.append(check_metadata_consistency(state))
    if state.get("_payload_validation_result_object"):
        checks.append(state["_payload_validation_result_object"])
    else:
        checks.append(check_payload_conformity(parsed_payload, state.get("claim_payload_type"), state.get("route", {})))
    checks.append(check_financial_consistency(state))

    state, eligibility = run_eligibility_subgraph(
        state=state,
        payer_rules=payer_rules,
        cache=cache,
        repository=repository,
        object_store=object_store,
    )
    checks.append(eligibility)
    if str(eligibility.status) == EligibilityStatus.WAITING_FOR_PAYER:
        return state, checks
    if str(eligibility.status) in {EligibilityStatus.PASS, EligibilityStatus.CACHED_VALID}:
        state, prior_auth = run_prior_auth_subgraph(
            state=state,
            payer_rules=payer_rules,
            kg_client=kg_client,
            repository=repository,
            pa_builder=pa_builder,
            object_store=object_store,
        )
    else:
        prior_auth = CheckResult("PRIOR_AUTH", "SKIPPED", data={"reason": "Eligibility did not pass."})
    checks.append(prior_auth)
    checks.append(check_coding_consistency(state, kg_client))
    checks.append(check_documentation(state, payer_rules, kg_client))
    checks.append(check_payer_rules(state, payer_rules, kg_client))
    checks.append(check_duplicate(state, repository))
    checks.append(check_submission_readiness(state, prior_auth))
    return state, checks


def calculate_validation_report(claim_id: str, checks: list[CheckResult]) -> ValidationReport:
    issues = [issue for check in checks for issue in check.issues]
    if any(
        str(check.status) in {EligibilityStatus.WAITING_FOR_PAYER, PriorAuthStatus.WAITING_FOR_PAYER}
        for check in checks
    ):
        return ValidationReport(claim_id, 100, ValidationStatus.WAITING_FOR_PAYER, issues, checks)
    if any(
        check.check_type == "PRIOR_AUTH" and check.data.get("payload_rebuild_required")
        for check in checks
    ):
        return ValidationReport(claim_id, 100, ValidationStatus.NEEDS_PAYLOAD_REBUILD, issues, checks)
    if any(issue.severity == Severity.CRITICAL for issue in issues):
        return ValidationReport(claim_id, 0, ValidationStatus.HOLD_CRITICAL, issues, checks)
    if any(issue.code == "PA_REQUIRED_MISSING" for issue in issues):
        score = max(0, 100 - sum(issue.penalty for issue in issues))
        return ValidationReport(claim_id, score, ValidationStatus.NEEDS_PRIOR_AUTH, issues, checks)
    if any(issue.check_type == "PAYLOAD_CONFORMITY" and issue.severity in {Severity.CRITICAL, Severity.ERROR} for issue in issues):
        score = max(0, 100 - sum(issue.penalty for issue in issues))
        return ValidationReport(claim_id, score, ValidationStatus.NEEDS_PAYLOAD_REBUILD, issues, checks)
    score = 100
    for issue in issues:
        if issue.severity == Severity.ERROR:
            score -= 20
        elif issue.severity == Severity.WARNING:
            score -= 5
        else:
            score -= issue.penalty
    score = max(0, score)
    status = ValidationStatus.READY_TO_SUBMIT if score >= 80 else ValidationStatus.NEEDS_REVIEW
    return ValidationReport(claim_id, score, status, issues, checks)
