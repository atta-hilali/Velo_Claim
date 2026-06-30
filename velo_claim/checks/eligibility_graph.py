from __future__ import annotations

import os
from typing import Any

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.builders.eligibility.nphies import NphiesEligibilityBuilder
from velo_claim.checks.eligibility import run_eligibility_check
from velo_claim.core.enums import ClaimStandard, EligibilityStatus, PayloadStatus, Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.fallback.waiting import enter_waiting_for_payer
from velo_claim.storage.interfaces import CacheStoreInterface, ObjectStoreInterface, RepositoryInterface


eligibility_checkpoint_store = MemoryCheckpointStore()


def build_eligibility_subgraph(
    *,
    payer_rules: PayerRuleSet,
    cache: CacheStoreInterface,
    repository: RepositoryInterface,
    object_store: ObjectStoreInterface | None = None,
    checkpoint_store: MemoryCheckpointStore | None = None,
):
    """Reusable Eligibility Check State Machine from the MD."""

    checkpoint_store = checkpoint_store or eligibility_checkpoint_store

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
        platform = state.get("eligibility_platform")
        try:
            standard = ClaimStandard(platform)
        except ValueError:
            standard = None
        if standard == ClaimStandard.NPHIES:
            payload = NphiesEligibilityBuilder().build(
                state.get("canonical_claim", {}),
                state.get("source_context", {}),
            )
            claim_id = state.get("canonical_claim", {}).get("claim_id") or "unknown"
            payload_state = {
                **state,
                "eligibility_payload": payload,
                "eligibility_payload_type": NphiesEligibilityBuilder.content_type,
                "eligibility_payload_standard": ClaimStandard.NPHIES,
            }
            if object_store:
                uri = object_store.put_text(
                    f"claims/{claim_id}/eligibility/payloads/1/payload.json",
                    payload,
                    content_type="application/fhir+json",
                )
                payload_state["eligibility_payload_uri"] = uri
            return payload_state
        return {
            **state,
            "eligibility_payload": {
                "patient_id": state["eligibility_input"]["patient_id"],
                "payer_id": state["eligibility_input"]["payer_id"],
                "service_date": state["eligibility_input"]["service_date"],
                "platform": state.get("eligibility_platform"),
            },
            "eligibility_payload_type": "application/json",
        }

    def submit_eligibility_request(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("eligibility_terminal"):
            return state
        if _callback_response(state):
            return {**state, "eligibility_request_status": "CALLBACK_RECEIVED"}
        if state.get("eligibility_requires_manual"):
            result = CheckResult(
                "ELIGIBILITY",
                EligibilityStatus.MANUAL_PORTAL_TASK,
                data={"task": "MANUAL_PORTAL_TASK", "payload": state.get("eligibility_payload")},
            )
            return {**state, "eligibility_result": result.to_dict(), "_eligibility_result_object": result, "eligibility_terminal": True}
        if _submit_to_payer_enabled(state):
            return _enter_waiting_state(
                state,
                request_status="WAITING_FOR_PAYER",
                message="Eligibility request was sent or queued for payer response.",
            )
        return {**state, "eligibility_request_status": "LOCAL_CONTEXT"}

    def poll_if_async(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("eligibility_terminal"):
            return state
        response = _callback_response(state)
        if response:
            return {
                **state,
                "eligibility_response": response,
                "eligibility_request_status": "RESPONSE_RECEIVED",
            }
        return state

    def parse_eligibility_response(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("eligibility_terminal"):
            return state
        response = state.get("eligibility_response")
        if not isinstance(response, dict):
            return state

        outcome = _response_outcome(response)
        if outcome in {"queued", "pending", "pended", "partial", "waiting"}:
            return _enter_waiting_state(
                state,
                request_status="WAITING_FOR_PAYER",
                message=f"Eligibility response is {outcome}; waiting for final payer response.",
            )
        if outcome in {"complete", "approved", "eligible", "active", "yes", "pass", "ok"}:
            result = CheckResult(
                "ELIGIBILITY",
                EligibilityStatus.PASS,
                data={
                    "payer_response": response,
                    "benefit_summary": response.get("benefit_summary") or response.get("benefits") or {},
                    "eligibility_ref": (
                        response.get("eligibility_ref")
                        or response.get("id_payer")
                        or response.get("authorization_id_payer")
                        or response.get("AuthorizationIDPayer")
                    ),
                },
            )
            cache.set(
                state["eligibility_input"]["cache_key"],
                result.to_dict(),
                ttl_seconds=payer_rules.eligibility_ttl_seconds,
            )
            return {
                **state,
                "eligibility_result": result.to_dict(),
                "_eligibility_result_object": result,
                "eligibility_terminal": True,
            }

        issue = CheckIssue(
            code="ELIGIBILITY_PAYER_REJECTED" if outcome in {"denied", "ineligible", "inactive", "no"} else "ELIGIBILITY_RESPONSE_UNRECOGNIZED",
            severity=Severity.CRITICAL if outcome in {"denied", "ineligible", "inactive", "no"} else Severity.ERROR,
            check_type="ELIGIBILITY",
            field="eligibility_response",
            message="Payer eligibility response does not confirm active eligibility.",
            suggestion="Review the payer response and resolve eligibility before continuing.",
            penalty=100 if outcome in {"denied", "ineligible", "inactive", "no"} else 20,
            evidence={"response": response},
        )
        result = CheckResult(
            "ELIGIBILITY",
            EligibilityStatus.FAIL_HOLD_CRITICAL,
            [issue],
            data={"payer_response": response},
        )
        return {
            **state,
            "eligibility_result": result.to_dict(),
            "_eligibility_result_object": result,
            "eligibility_terminal": True,
        }

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
                    "eligibility_ref": result.get("data", {}).get("eligibility_ref"),
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

    def _enter_waiting_state(state: dict[str, Any], *, request_status: str, message: str) -> dict[str, Any]:
        claim_id = state.get("canonical_claim", {}).get("claim_id") or state.get("claim", {}).get("claim_id")
        thread_id = state.get("thread_id") or f"eligibility:{claim_id or 'unknown'}"
        waiting_input = {
            **state,
            "claim": state.get("claim") or {"claim_id": claim_id},
            "eligibility_request_status": request_status,
        }
        waiting_state = enter_waiting_for_payer(
            state=waiting_input,
            cache=cache,
            agent="EligibilityCheckSubgraph",
            node="submit_eligibility_request",
            thread_id=thread_id,
            resume_node="parse_eligibility_response",
            checkpoint_store=checkpoint_store,
        )
        result = CheckResult(
            "ELIGIBILITY",
            EligibilityStatus.WAITING_FOR_PAYER,
            data={
                "request_status": request_status,
                "message": message,
                "payload": state.get("eligibility_payload"),
                "callback_state": waiting_state.get("callback_state", {}),
            },
        )
        return {
            **waiting_state,
            "eligibility_result": result.to_dict(),
            "_eligibility_result_object": result,
            "eligibility_terminal": True,
        }

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
    checkpoint_store: MemoryCheckpointStore | None = None,
) -> tuple[dict[str, Any], CheckResult]:
    result_state = build_eligibility_subgraph(
        payer_rules=payer_rules,
        cache=cache,
        repository=repository,
        object_store=object_store,
        checkpoint_store=checkpoint_store,
    ).invoke(state)
    return result_state, result_state["_eligibility_result_object"]


def _submit_to_payer_enabled(state: dict[str, Any]) -> bool:
    value = state.get("eligibility_submit_to_payer")
    if value is None:
        value = os.getenv("ELIGIBILITY_AUTO_SUBMIT", "false")
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _callback_response(state: dict[str, Any]) -> dict[str, Any] | None:
    response = state.get("eligibility_response")
    if isinstance(response, dict):
        return response
    callback_results = state.get("callback_results") or {}
    for key in ("parse_eligibility_response", "eligibility", "eligibility_response"):
        response = callback_results.get(key)
        if isinstance(response, dict):
            return response
    return None


def _response_outcome(response: dict[str, Any]) -> str:
    for key in ("outcome", "status", "result", "eligibility_status", "decision"):
        value = response.get(key)
        if value:
            return str(value).strip().lower()
    if response.get("eligible") is True or response.get("is_eligible") is True:
        return "eligible"
    if response.get("eligible") is False or response.get("is_eligible") is False:
        return "ineligible"
    return "unknown"
