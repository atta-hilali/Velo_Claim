from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.checks.prior_auth import auth_valid
from velo_claim.core.enums import PriorAuthStatus, Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.engine import pa_required_for_code
from velo_claim.storage.interfaces import ObjectStoreInterface, RepositoryInterface


def build_prior_auth_subgraph(
    *,
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
    repository: RepositoryInterface,
    pa_builder: PAClaimBuilderModule,
    object_store: ObjectStoreInterface | None = None,
):
    """Reusable Prior Auth Check State Machine from the MD."""

    def normalize_prior_auth_input(state: dict[str, Any]) -> dict[str, Any]:
        claim = state.get("canonical_claim", {})
        return {
            **state,
            "prior_auth_input": {
                "activities": [
                    {
                        "code": line.get("code"),
                        "service_date": claim.get("encounter", {}).get("service_date"),
                        "payer_id": claim.get("payer", {}).get("id"),
                        "plan_id": claim.get("payer", {}).get("plan_id"),
                    }
                    for line in claim.get("line_items", [])
                    if line.get("code")
                ]
            },
        }

    def determine_requirement(state: dict[str, Any]) -> dict[str, Any]:
        claim = state.get("canonical_claim", {})
        payer = claim.get("payer", {})
        required_codes = [
            activity["code"]
            for activity in state.get("prior_auth_input", {}).get("activities", [])
            if pa_required_for_code(
                payer_id=payer.get("id", ""),
                plan_id=payer.get("plan_id", ""),
                cpt_code=activity["code"],
                payer_rules=payer_rules,
                kg_client=kg_client,
            )
        ]
        if not required_codes:
            result = CheckResult("PRIOR_AUTH", PriorAuthStatus.NOT_REQUIRED, data={"required_codes": []})
            return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result, "prior_auth_terminal": True}
        return {**state, "prior_auth_required_codes": required_codes, "prior_auth_terminal": False}

    def validate_existing_auth(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal"):
            return state
        claim = state.get("canonical_claim", {})
        payer = claim.get("payer", {})
        valid_refs: list[str] = []
        issues: list[CheckIssue] = []
        missing_codes: list[str] = []
        for code in state.get("prior_auth_required_codes", []):
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
        if issues:
            result = CheckResult("PRIOR_AUTH", PriorAuthStatus.HOLD_CRITICAL, issues)
            return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result, "prior_auth_terminal": True}
        if valid_refs and not missing_codes:
            result = CheckResult("PRIOR_AUTH", PriorAuthStatus.ALREADY_VALID, data={"valid_refs": valid_refs})
            return {
                **state,
                "canonical_claim": {**claim, "pre_auth_ref": valid_refs[0]},
                "prior_auth_result": result.to_dict(),
                "_prior_auth_result_object": result,
                "prior_auth_terminal": True,
            }
        return {**state, "prior_auth_missing_codes": missing_codes, "prior_auth_valid_refs": valid_refs}

    def pa_route_decision(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal"):
            return state
        existing = None
        if hasattr(repository, "prior_auth_requests"):
            for request in getattr(repository, "prior_auth_requests", {}).values():
                if request.get("claim_id") == state.get("canonical_claim", {}).get("claim_id") and request.get("required_codes") == state.get("prior_auth_missing_codes"):
                    existing = request
                    break
        return {**state, "prior_auth_existing_request": existing}

    def pa_claim_builder(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal") or state.get("prior_auth_existing_request"):
            return state
        return pa_builder.build(state, state.get("prior_auth_missing_codes", []))

    def submit_or_create_task(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal"):
            return state
        if state.get("prior_auth_existing_request"):
            return {**state, "prior_auth_request_id": state["prior_auth_existing_request"].get("request_id")}
        claim = state.get("canonical_claim", {})
        request_id = repository.insert_prior_auth_request(
            claim["claim_id"],
            {
                "standard": state.get("route", {}).get("prior_auth_standard"),
                "object_uri": state.get("pa_payload_uri"),
                "status": PriorAuthStatus.REQUIRED_MISSING,
                "required_codes": state.get("prior_auth_missing_codes", []),
            },
        )
        return {**state, "prior_auth_request_id": request_id, "prior_auth_submit_status": "MANUAL_PORTAL_TASK"}

    def poll_if_needed(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def parse_final_response(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def patch_claim(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_valid_refs"):
            return {
                **state,
                "canonical_claim": {**state.get("canonical_claim", {}), "pre_auth_ref": state["prior_auth_valid_refs"][0]},
                "payload_rebuild_required": True,
            }
        return state

    def finish(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("_prior_auth_result_object"):
            return state
        missing_codes = state.get("prior_auth_missing_codes", [])
        issues = []
        if missing_codes:
            issues.append(
                CheckIssue(
                    code="PA_REQUIRED_MISSING",
                    severity=Severity.ERROR,
                    check_type="PRIOR_AUTH",
                    field="canonical_claim.procedures",
                    message=f"Prior authorization is required for {', '.join(missing_codes)}.",
                    suggestion="Submit the generated PA payload and wait for approval.",
                    penalty=20,
                    evidence={"request_id": state.get("prior_auth_request_id"), "pa_payload_uri": state.get("pa_payload_uri")},
                )
            )
        result = CheckResult(
            "PRIOR_AUTH",
            PriorAuthStatus.REQUIRED_MISSING if issues else PriorAuthStatus.ALREADY_VALID,
            issues,
            {"required_codes": state.get("prior_auth_required_codes", []), "valid_refs": state.get("prior_auth_valid_refs", [])},
        )
        return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result}

    graph = StateGraph(dict)
    nodes = {
        "normalize_prior_auth_input": normalize_prior_auth_input,
        "determine_requirement": determine_requirement,
        "validate_existing_auth": validate_existing_auth,
        "pa_route_decision": pa_route_decision,
        "pa_claim_builder": pa_claim_builder,
        "submit_or_create_task": submit_or_create_task,
        "poll_if_needed": poll_if_needed,
        "parse_final_response": parse_final_response,
        "patch_claim": patch_claim,
        "finish": finish,
    }
    for name, fn in nodes.items():
        graph.add_node(
            name,
            audited_node(
                agent="PriorAuthCheckSubgraph",
                node=name,
                fn=fn,
                repository=repository,
                object_store=object_store,
            ),
        )
    graph.add_edge(START, "normalize_prior_auth_input")
    graph.add_edge("normalize_prior_auth_input", "determine_requirement")
    graph.add_edge("determine_requirement", "validate_existing_auth")
    graph.add_edge("validate_existing_auth", "pa_route_decision")
    graph.add_edge("pa_route_decision", "pa_claim_builder")
    graph.add_edge("pa_claim_builder", "submit_or_create_task")
    graph.add_edge("submit_or_create_task", "poll_if_needed")
    graph.add_edge("poll_if_needed", "parse_final_response")
    graph.add_edge("parse_final_response", "patch_claim")
    graph.add_edge("patch_claim", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


def run_prior_auth_subgraph(
    *,
    state: dict[str, Any],
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
    repository: RepositoryInterface,
    pa_builder: PAClaimBuilderModule,
    object_store: ObjectStoreInterface | None = None,
) -> tuple[dict[str, Any], CheckResult]:
    result_state = build_prior_auth_subgraph(
        payer_rules=payer_rules,
        kg_client=kg_client,
        repository=repository,
        pa_builder=pa_builder,
        object_store=object_store,
    ).invoke(state)
    return result_state, result_state["_prior_auth_result_object"]
