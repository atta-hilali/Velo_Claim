from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.checks.prior_auth_graph import run_prior_auth_subgraph
from velo_claim.core.container import ServiceContainer, build_default_container
from velo_claim.core.models import RoutingContext


def build_prior_authorization_agent(*, container: ServiceContainer | None = None):
    services = container or build_default_container()
    pa_builder = PAClaimBuilderModule(repository=services.repository, object_store=services.object_store)

    def run_subgraph(state: dict) -> dict:
        routing = RoutingContext(**state.get("routing_context", {}))
        payer_rules = services.payer_rule_loader.load(routing.payer_id, routing.plan_id)
        state, result = run_prior_auth_subgraph(
            state=state,
            payer_rules=payer_rules,
            kg_client=services.kg_client,
            repository=services.repository,
            pa_builder=pa_builder,
            object_store=services.object_store,
        )
        return {**state, "prior_auth_result": result.to_dict()}

    graph = StateGraph(dict)
    graph.add_node(
        "prior_auth_check_subgraph",
        audited_node(
            agent="PriorAuthorizationAgent",
            node="prior_auth_check_subgraph",
            fn=run_subgraph,
            repository=services.repository,
            object_store=services.object_store,
        ),
    )
    graph.add_edge(START, "prior_auth_check_subgraph")
    graph.add_edge("prior_auth_check_subgraph", END)
    return graph.compile()


def run_prior_authorization(initial_state: dict, *, container: ServiceContainer | None = None) -> dict:
    return build_prior_authorization_agent(container=container).invoke(initial_state)
