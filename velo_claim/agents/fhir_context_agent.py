from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.context.adapters import AdapterInterface
from velo_claim.context.fhir_context import resolve_fhir_context
from velo_claim.context.input_resolver import InputResolver
from velo_claim.context.vendor_bridge import adapter_from_env
from velo_claim.core.container import ServiceContainer, build_default_container
from velo_claim.routing.router import ClaimRouter


def build_fhir_context_agent(
    *,
    container: ServiceContainer | None = None,
    adapter: AdapterInterface | None = None,
):
    services = container or build_default_container()
    resolver = InputResolver()
    router = ClaimRouter(services.repository)

    def normalize_input(state: dict[str, Any]) -> dict[str, Any]:
        return resolver.resolve(state)

    def load_context(state: dict[str, Any]) -> dict[str, Any]:
        effective_adapter = adapter
        should_use_vendor = bool(
            state.get("use_vendor_fhir_adapter")
            or os.getenv("FHIR_BASE_URL")
            or os.getenv("FHIR_ADAPTER")
            or os.getenv("EHR_ADAPTER")
        )
        if effective_adapter is None and should_use_vendor:
            effective_adapter = adapter_from_env(state, cache=services.cache)
        return resolve_fhir_context(state, effective_adapter)

    def route_context(state: dict[str, Any]) -> dict[str, Any]:
        claim_id = state.get("claim", {}).get("claim_id") or state.get("claim_id") or f"CLM-{uuid4().hex[:12].upper()}"
        route = router.decide(claim_id, state["routing_context"])
        return {
            **state,
            "claim": {**state.get("claim", {}), "claim_id": claim_id},
            "route": route.to_dict(),
            "claim_format": route.claim_standard,
            "jurisdiction": route.jurisdiction,
        }

    graph = StateGraph(dict)
    graph.add_node("normalize_input", audited_node(agent="FHIRContextAgent", node="normalize_input", fn=normalize_input, repository=services.repository, object_store=services.object_store))
    graph.add_node("load_context", audited_node(agent="FHIRContextAgent", node="load_context", fn=load_context, repository=services.repository, object_store=services.object_store))
    graph.add_node("route_context", audited_node(agent="FHIRContextAgent", node="route_context", fn=route_context, repository=services.repository, object_store=services.object_store))
    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "load_context")
    graph.add_edge("load_context", "route_context")
    graph.add_edge("route_context", END)
    return graph.compile()


def run_fhir_context(
    initial_state: dict[str, Any],
    *,
    container: ServiceContainer | None = None,
    adapter: AdapterInterface | None = None,
) -> dict[str, Any]:
    return build_fhir_context_agent(container=container, adapter=adapter).invoke(initial_state)
