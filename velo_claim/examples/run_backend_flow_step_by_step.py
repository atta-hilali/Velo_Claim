from __future__ import annotations

from pprint import pprint

from velo_claim.agents.claim_preparation import run_claim_preparation
from velo_claim.agents.claim_validation import run_claim_validation
from velo_claim.agents.fhir_context_agent import run_fhir_context
from velo_claim.core.container import build_default_container
from velo_claim.examples.demo_inputs import abu_dhabi_pneumonia_encounter


def preview(value: object, limit: int = 900) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}\n... <truncated>"


def print_title(title: str) -> None:
    print(f"\n=== {title} ===")


def run_step_by_step() -> dict:
    services = build_default_container()
    state0 = abu_dhabi_pneumonia_encounter()

    print_title("STEP 0: INPUT ENCOUNTER PACKAGE")
    pprint(
        {
            "claim_id": state0.get("claim_id"),
            "has_source_context": bool(state0.get("source_context")),
            "source_resources": sorted(state0.get("source_context", {}).keys()),
        }
    )

    state1 = run_fhir_context(state0, container=services)
    print_title("STEP 1: FHIR CONTEXT + ROUTER")
    pprint(
        {
            "claim": state1.get("claim"),
            "routing_context": state1.get("routing_context"),
            "route": state1.get("route"),
            "claim_format": str(state1.get("claim_format")),
            "jurisdiction": str(state1.get("jurisdiction")),
            "source_context_keys": sorted(state1.get("source_context", {}).keys()),
        }
    )

    state2 = run_claim_preparation(state1, container=services)
    print_title("STEP 2: CLAIM PREPARATION")
    pprint(
        {
            "claim": state2.get("claim"),
            "canonical_claim_keys": sorted(state2.get("canonical_claim", {}).keys()),
            "claim_payload_type": state2.get("claim_payload_type"),
            "claim_payload_uri": state2.get("claim_payload_uri"),
            "payload_status": str(state2.get("payload_status")),
            "payload_version": state2.get("payload_version"),
            "next_agent": state2.get("next_agent"),
        }
    )
    print("\n--- Payload preview ---")
    print(preview(state2.get("claim_payload")))

    state3 = run_claim_validation(state2, container=services)
    print_title("STEP 3: CLAIM VALIDATION")
    pprint(
        {
            "payload_status": str(state3.get("payload_status")),
            "final_status": str(state3.get("final_status")),
            "score": state3.get("score"),
            "next_agent": state3.get("next_agent"),
            "validation_report_uri": state3.get("validation_report_uri"),
            "errors_count": len(state3.get("errors", [])),
            "warnings_count": len(state3.get("warnings", [])),
        }
    )

    print("\n--- Validation checks ---")
    for check in state3.get("validation_report", {}).get("checks", []):
        print(
            f"{check['check_type']}: {check['status']} | "
            f"passes={check['passes']} | issues={len(check['issues'])}"
        )
        for issue in check.get("issues", []):
            print(f"  - {issue['severity']} {issue['code']}: {issue['message']}")

    print_title("STEP 4: STORAGE SNAPSHOT")
    pprint(
        {
            "claims": list(services.repository.claims.keys()),
            "route_decisions": list(services.repository.route_decisions.keys()),
            "claim_payloads": len(services.repository.claim_payloads),
            "claim_versions": len(services.repository.claim_versions),
            "validation_reports": len(services.repository.validation_reports),
            "validation_issues": len(services.repository.validation_issues),
            "audit_events": len(services.repository.audit_events),
            "object_store_objects": len(services.object_store.objects),
        }
    )

    return state3


if __name__ == "__main__":
    run_step_by_step()
