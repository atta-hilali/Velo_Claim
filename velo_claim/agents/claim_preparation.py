from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.core.container import ServiceContainer, build_default_container
from velo_claim.core.enums import PayloadStatus


def build_claim_preparation_agent(*, container: ServiceContainer | None = None):
    services = container or build_default_container()
    builder = ClaimBuilderModule(
        repository=services.repository,
        object_store=services.object_store,
        kg_client=services.kg_client,
        payer_rule_loader=services.payer_rule_loader,
    )

    def supervisor(state: dict[str, Any]) -> dict[str, Any]:
        claim_id = state.get("claim", {}).get("claim_id")
        if not claim_id:
            raise ValueError("Claim Preparation requires claim.claim_id from the context/router stage.")
        services.repository.upsert_claim(
            claim_id,
            {
                "claim_id": claim_id,
                "status": PayloadStatus.DRAFT_BUILDING,
                "jurisdiction": state.get("route", {}).get("jurisdiction"),
                "payer_id": state.get("routing_context", {}).get("payer_id"),
                "provider_id": state.get("routing_context", {}).get("provider_license"),
                "patient_id": state.get("source_context", {}).get("patient", {}).get("id"),
            },
        )
        return {**state, "payload_status": PayloadStatus.DRAFT_BUILDING}

    def prepare_claim(state: dict[str, Any]) -> dict[str, Any]:
        return builder.build(state)

    def validate_claim(state: dict[str, Any]) -> dict[str, Any]:
        if not state.get("claim_payload"):
            return {**state, "payload_status": PayloadStatus.DRAFT_INVALID}
        return {
            **state,
            "payload_status": PayloadStatus.DRAFT_BUILT,
            "next_agent": "ClaimValidationAgent",
        }

    graph = StateGraph(dict)
    graph.add_node("supervisor", audited_node(agent="ClaimPreparationAgent", node="supervisor", fn=supervisor, repository=services.repository, object_store=services.object_store))
    graph.add_node("prepare_claim", audited_node(agent="ClaimPreparationAgent", node="prepare_claim", fn=prepare_claim, repository=services.repository, object_store=services.object_store))
    graph.add_node("validate_claim", audited_node(agent="ClaimPreparationAgent", node="validate_claim", fn=validate_claim, repository=services.repository, object_store=services.object_store))
    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", "prepare_claim")
    graph.add_edge("prepare_claim", "validate_claim")
    graph.add_edge("validate_claim", END)
    return graph.compile()


def run_claim_preparation(
    initial_state: dict[str, Any],
    *,
    container: ServiceContainer | None = None,
) -> dict[str, Any]:
    return build_claim_preparation_agent(container=container).invoke(initial_state)
