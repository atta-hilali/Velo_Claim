from __future__ import annotations

from velo_claim.agents.claim_preparation import run_claim_preparation
from velo_claim.agents.claim_validation import run_claim_validation
from velo_claim.agents.fhir_context_agent import run_fhir_context
from velo_claim.context.adapters import AdapterInterface
from velo_claim.core.container import ServiceContainer, build_default_container


def run_full_pipeline(
    initial_state: dict,
    *,
    container: ServiceContainer | None = None,
    adapter: AdapterInterface | None = None,
) -> dict:
    services = container or build_default_container()
    state = run_fhir_context(initial_state, container=services, adapter=adapter)
    state = run_claim_preparation(state, container=services)
    state = run_claim_validation(state, container=services)
    return state
