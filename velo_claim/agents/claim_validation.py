from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.checks.orchestrator import calculate_validation_report, run_validation_checks
from velo_claim.core.container import ServiceContainer, build_default_container
from velo_claim.core.enums import PayloadStatus, Severity
from velo_claim.core.models import RoutingContext
from velo_claim.validation.payload_validators import PayloadValidator


def build_claim_validation_agent(*, container: ServiceContainer | None = None):
    services = container or build_default_container()
    pa_builder = PAClaimBuilderModule(repository=services.repository, object_store=services.object_store)
    claim_builder = ClaimBuilderModule(
        repository=services.repository,
        object_store=services.object_store,
        kg_client=services.kg_client,
        payer_rule_loader=services.payer_rule_loader,
    )
    payload_validator = PayloadValidator()

    def normalize_validation_input(state: dict[str, Any]) -> dict[str, Any]:
        claim_id = state.get("claim", {}).get("claim_id")
        if not claim_id:
            raise ValueError("Validation requires claim.claim_id.")
        route_count = services.repository.count_route_decisions(claim_id)
        if route_count != 1:
            raise ValueError(f"Expected exactly one route decision for claim {claim_id}, found {route_count}.")
        route_row = services.repository.get_route_decision(claim_id)
        if int(state.get("rebuild_attempt_count") or 0) >= 3:
            return {**state, "payload_status": PayloadStatus.HOLD_CRITICAL}
        payload = state.get("claim_payload")
        if not payload and state.get("claim_payload_uri"):
            payload = services.object_store.get_text(state["claim_payload_uri"])
        return {
            **state,
            "route": route_row["route"],
            "claim_payload": payload,
            "validation_scope_errors": [],
            "validation_scope_warnings": [],
        }

    def load_route_and_context(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def load_payer_rule(state: dict[str, Any]) -> dict[str, Any]:
        routing = RoutingContext(**state.get("routing_context", {}))
        rule_set = services.payer_rule_loader.load(routing.payer_id, routing.plan_id)
        warnings = list(state.get("warnings", []))
        if rule_set.source in {"MOCK", "CACHED"}:
            warnings.append(
                {
                    "code": "PAYER_RULE_SOURCE_NOT_LIVE",
                    "message": f"Payer rules came from {rule_set.source}.",
                    "payer_id": routing.payer_id,
                    "plan_id": routing.plan_id,
                }
            )
        return {**state, "payer_rule_set": rule_set.to_dict(), "warnings": warnings}

    def parse_claim_payload(state: dict[str, Any]) -> dict[str, Any]:
        parsed, parse_result = payload_validator.validate(
            payload=state.get("claim_payload") or "",
            payload_type=state.get("claim_payload_type") or "",
            route=state.get("route", {}),
        )
        return {
            **state,
            "parsed_payload": parsed,
            "parse_result": parse_result.to_dict(),
            "_payload_validation_result_object": parse_result,
        }

    def validation_node(state: dict[str, Any]) -> dict[str, Any]:
        routing = RoutingContext(**state.get("routing_context", {}))
        payer_rules = services.payer_rule_loader.load(routing.payer_id, routing.plan_id)
        state, checks = run_validation_checks(
            state=state,
            parsed_payload=state.get("parsed_payload"),
            payer_rules=payer_rules,
            kg_client=services.kg_client,
            repository=services.repository,
            object_store=services.object_store,
            cache=services.cache,
            pa_builder=pa_builder,
        )
        return {**state, "validation_checks": [check.to_dict() for check in checks], "_validation_check_objects": checks}

    def calculate_score(state: dict[str, Any]) -> dict[str, Any]:
        claim_id = state["canonical_claim"]["claim_id"]
        checks = state["_validation_check_objects"]
        report = calculate_validation_report(claim_id, checks)
        report_dict = report.to_dict()
        report_id = services.repository.insert_validation_report(
            claim_id,
            {
                "version": state.get("payload_version", 1),
                "score": report.score,
                "final_status": report.status,
                "report": report_dict,
            },
        )
        for issue in report.issues:
            services.repository.insert_validation_issue(report_id, issue.to_dict())
        report_uri = services.object_store.put_text(
            f"claims/{claim_id}/validation_reports/{report_id}.json",
            __import__("json").dumps(report_dict, indent=2, default=str),
            content_type="application/json",
        )
        state_errors = list(state.get("errors", []))
        state_warnings = list(state.get("warnings", []))
        for issue in report.issues:
            item = issue.to_error("ClaimValidationAgent", "validation_node").to_dict()
            if issue.severity in {Severity.CRITICAL, Severity.ERROR}:
                state_errors.append(item)
            else:
                state_warnings.append(item)
        return {
            **state,
            "validation_report": report_dict,
            "validation_report_uri": report_uri,
            "score": report.score,
            "final_status": report.status,
            "errors": state_errors,
            "warnings": state_warnings,
        }

    def decision_router(state: dict[str, Any]) -> dict[str, Any]:
        final_status = state.get("final_status")
        payload_status = {
            "READY_TO_SUBMIT": PayloadStatus.READY_TO_SUBMIT,
            "WAITING_FOR_PAYER": PayloadStatus.WAITING_FOR_PAYER,
            "NEEDS_PRIOR_AUTH": PayloadStatus.NEEDS_PRIOR_AUTH,
            "NEEDS_PAYLOAD_REBUILD": PayloadStatus.NEEDS_PAYLOAD_REBUILD,
            "HOLD_CRITICAL": PayloadStatus.HOLD_CRITICAL,
        }.get(str(final_status), PayloadStatus.NEEDS_REVIEW)
        return {**state, "payload_status": payload_status, "next_agent": "SubmissionAgent" if payload_status == PayloadStatus.READY_TO_SUBMIT else None}

    def fallback_rebuild(state: dict[str, Any]) -> dict[str, Any]:
        attempt = int(state.get("rebuild_attempt_count") or 0) + 1
        if attempt > 3:
            return {**state, "payload_status": PayloadStatus.HOLD_CRITICAL, "rebuild_attempt_count": attempt}
        rebuilt = claim_builder.build({**state, "rebuild_attempt_count": attempt})
        return {**rebuilt, "rebuild_attempt_count": attempt, "payload_status": PayloadStatus.DRAFT_BUILT}

    def route_after_score(state: dict[str, Any]) -> str:
        if str(state.get("final_status")) == "NEEDS_PAYLOAD_REBUILD" and int(state.get("rebuild_attempt_count") or 0) < 3:
            return "fallback"
        return "decide"

    graph = StateGraph(dict)
    graph.add_node("normalize_validation_input", audited_node(agent="ClaimValidationAgent", node="normalize_validation_input", fn=normalize_validation_input, repository=services.repository, object_store=services.object_store))
    graph.add_node("load_route_and_context", audited_node(agent="ClaimValidationAgent", node="load_route_and_context", fn=load_route_and_context, repository=services.repository, object_store=services.object_store))
    graph.add_node("load_payer_rule", audited_node(agent="ClaimValidationAgent", node="load_payer_rule", fn=load_payer_rule, repository=services.repository, object_store=services.object_store))
    graph.add_node("parse_payload", audited_node(agent="ClaimValidationAgent", node="parse_payload", fn=parse_claim_payload, repository=services.repository, object_store=services.object_store))
    graph.add_node("validation_node", audited_node(agent="ClaimValidationAgent", node="validation_node", fn=validation_node, repository=services.repository, object_store=services.object_store))
    graph.add_node("calculate_score", audited_node(agent="ClaimValidationAgent", node="calculate_score", fn=calculate_score, repository=services.repository, object_store=services.object_store))
    graph.add_node("fallback_rebuild", audited_node(agent="ClaimValidationAgent", node="fallback_rebuild", fn=fallback_rebuild, repository=services.repository, object_store=services.object_store))
    graph.add_node("decision_router", audited_node(agent="ClaimValidationAgent", node="decision_router", fn=decision_router, repository=services.repository, object_store=services.object_store))
    graph.add_edge(START, "normalize_validation_input")
    graph.add_edge("normalize_validation_input", "load_route_and_context")
    graph.add_edge("load_route_and_context", "load_payer_rule")
    graph.add_edge("load_payer_rule", "parse_payload")
    graph.add_edge("parse_payload", "validation_node")
    graph.add_edge("validation_node", "calculate_score")
    graph.add_conditional_edges("calculate_score", route_after_score, {"fallback": "fallback_rebuild", "decide": "decision_router"})
    graph.add_edge("fallback_rebuild", "parse_payload")
    graph.add_edge("decision_router", END)
    return graph.compile()


def run_claim_validation(initial_state: dict[str, Any], *, container: ServiceContainer | None = None) -> dict[str, Any]:
    return build_claim_validation_agent(container=container).invoke(initial_state)
