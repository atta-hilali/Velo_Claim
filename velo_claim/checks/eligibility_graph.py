from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.checks.eligibility import run_eligibility_check
from velo_claim.core.enums import EligibilityStatus
from velo_claim.core.models import CheckResult, PayerRuleSet
from velo_claim.storage.interfaces import CacheStoreInterface, ObjectStoreInterface, RepositoryInterface


def build_eligibility_subgraph(
    *,
    payer_rules: PayerRuleSet,
    cache: CacheStoreInterface,
    repository: RepositoryInterface,
    object_store: ObjectStoreInterface | None = None,
):
    """Reusable Eligibility Check State Machine from the MD."""

    def normalize_eligibility_input(state: dict[str, Any]) -> dict[str, Any]:
        claim = state.get("canonical_claim", {})
        patient_id = claim.get("patient", {}).get("id")
        payer_id = claim.get("payer", {}).get("id")
        service_date = claim.get("encounter", {}).get("service_date")
        return {
            **state,
            "eligibility_input": {
                "patient_id": patient_id,
                "payer_id": payer_id,
                "service_date": service_date,
                "cache_key": f"eligibility:{patient_id}:{payer_id}:{service_date}",
            },
        }

    def check_cached_eligibility(state: dict[str, Any]) -> dict[str, Any]:
        cached = cache.get(state["eligibility_input"]["cache_key"])
        if cached:
            return {
                **state,
                "eligibility_result": cached,
                "eligibility_terminal": True,
                "eligibility_status": EligibilityStatus.CACHED_VALID,
            }
        return {**state, "eligibility_terminal": False}

    def determine_eligibility_requirement(state: dict[str, Any]) -> dict[str, Any]:
        requires_manual = state.get("route", {}).get("eligibility_profile") == "MANUAL_PORTAL"
        return {**state, "eligibility_requires_manual": requires_manual}

    def route_to_platform(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("eligibility_terminal"):
            return state
        return {**state, "eligibility_platform": state.get("route", {}).get("claim_standard", "MANUAL")}

    def build_eligibility_payload(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("eligibility_terminal"):
            return state
        return {
            **state,
            "eligibility_payload": {
                "patient_id": state["eligibility_input"]["patient_id"],
                "payer_id": state["eligibility_input"]["payer_id"],
                "service_date": state["eligibility_input"]["service_date"],
                "platform": state.get("eligibility_platform"),
            },
        }

    def submit_eligibility_request(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("eligibility_terminal"):
            return state
        if state.get("eligibility_requires_manual"):
            result = CheckResult(
                "ELIGIBILITY",
                EligibilityStatus.MANUAL_PORTAL_TASK,
                data={"task": "MANUAL_PORTAL_TASK", "payload": state.get("eligibility_payload")},
            )
            return {**state, "eligibility_result": result.to_dict(), "_eligibility_result_object": result, "eligibility_terminal": True}
        return {**state, "eligibility_request_status": "LOCAL_CONTEXT"}

    def poll_if_async(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def parse_eligibility_response(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def validate_coverage_details(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("eligibility_terminal"):
            return state
        result = run_eligibility_check(state, payer_rules, cache)
        return {**state, "eligibility_result": result.to_dict(), "_eligibility_result_object": result}

    def patch_claim_with_coverage(state: dict[str, Any]) -> dict[str, Any]:
        claim = state.get("canonical_claim", {})
        result = state.get("eligibility_result", {})
        if result.get("passes"):
            claim = {
                **claim,
                "payer": {
                    **claim.get("payer", {}),
                    "eligibility_status": result.get("status"),
                    "benefit_summary": result.get("data", {}).get("benefit_summary", {}),
                },
            }
        return {**state, "canonical_claim": claim}

    def store_eligibility_record(state: dict[str, Any]) -> dict[str, Any]:
        if hasattr(repository, "eligibility_checks"):
            repository.eligibility_checks.append(
                {
                    "claim_id": state.get("canonical_claim", {}).get("claim_id"),
                    "input": state.get("eligibility_input", {}),
                    "result": state.get("eligibility_result", {}),
                }
            )
        return state

    def finish(state: dict[str, Any]) -> dict[str, Any]:
        if "_eligibility_result_object" not in state and state.get("eligibility_result"):
            result = state["eligibility_result"]
            return {
                **state,
                "_eligibility_result_object": CheckResult(
                    "ELIGIBILITY",
                    result.get("status", "UNKNOWN"),
                    data=result.get("data", {}),
                ),
            }
        return state

    graph = StateGraph(dict)
    for name, fn in {
        "normalize_eligibility_input": normalize_eligibility_input,
        "check_cached_eligibility": check_cached_eligibility,
        "determine_eligibility_requirement": determine_eligibility_requirement,
        "route_to_platform": route_to_platform,
        "build_eligibility_payload": build_eligibility_payload,
        "submit_eligibility_request": submit_eligibility_request,
        "poll_if_async": poll_if_async,
        "parse_eligibility_response": parse_eligibility_response,
        "validate_coverage_details": validate_coverage_details,
        "patch_claim_with_coverage": patch_claim_with_coverage,
        "store_eligibility_record": store_eligibility_record,
        "finish": finish,
    }.items():
        graph.add_node(
            name,
            audited_node(
                agent="EligibilityCheckSubgraph",
                node=name,
                fn=fn,
                repository=repository,
                object_store=object_store,
            ),
        )
    graph.add_edge(START, "normalize_eligibility_input")
    graph.add_edge("normalize_eligibility_input", "check_cached_eligibility")
    graph.add_edge("check_cached_eligibility", "determine_eligibility_requirement")
    graph.add_edge("determine_eligibility_requirement", "route_to_platform")
    graph.add_edge("route_to_platform", "build_eligibility_payload")
    graph.add_edge("build_eligibility_payload", "submit_eligibility_request")
    graph.add_edge("submit_eligibility_request", "poll_if_async")
    graph.add_edge("poll_if_async", "parse_eligibility_response")
    graph.add_edge("parse_eligibility_response", "validate_coverage_details")
    graph.add_edge("validate_coverage_details", "patch_claim_with_coverage")
    graph.add_edge("patch_claim_with_coverage", "store_eligibility_record")
    graph.add_edge("store_eligibility_record", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


def run_eligibility_subgraph(
    *,
    state: dict[str, Any],
    payer_rules: PayerRuleSet,
    cache: CacheStoreInterface,
    repository: RepositoryInterface,
    object_store: ObjectStoreInterface | None = None,
) -> tuple[dict[str, Any], CheckResult]:
    result_state = build_eligibility_subgraph(
        payer_rules=payer_rules,
        cache=cache,
        repository=repository,
        object_store=object_store,
    ).invoke(state)
    return result_state, result_state["_eligibility_result_object"]
