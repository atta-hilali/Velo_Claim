from velo_claim.core.container import build_default_container
from velo_claim.core.enums import PayloadStatus
from velo_claim.core.models import RoutingContext
from velo_claim.examples.demo_inputs import abu_dhabi_pneumonia_encounter
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.jobs.poll_worker import process_poll_job
from velo_claim.pipeline import run_full_pipeline
from velo_claim.routing.router import ClaimRouter


def test_clean_rebuild_full_pipeline():
    container = build_default_container()
    result = run_full_pipeline(abu_dhabi_pneumonia_encounter(), container=container)

    assert result["route"]["claim_standard"] == "SHAFAFIYA"
    assert result["claim_payload_type"] == "application/xml"
    assert "<Claim.Submission>" in result["claim_payload"]
    assert result["validation_report"]["status"] == "READY_TO_SUBMIT"
    assert result["payload_status"] == PayloadStatus.READY_TO_SUBMIT
    assert container.repository.get_route_decision("CLM-CLEAN-AUH-001") is not None
    assert container.repository.latest_claim_payload("CLM-CLEAN-AUH-001") is not None
    assert {"NODE_ENTER", "NODE_EXIT"} <= {event["event_type"] for event in container.repository.audit_events}
    assert any("/audit/" in uri for uri in container.object_store.objects)


def test_payer_registry_drives_route_decision():
    container = build_default_container()
    route = ClaimRouter(container.repository).decide(
        "CLM-REGISTRY-DXB-001",
        RoutingContext(payer_name="Oman Insurance", payer_id="OIC UAE"),
    )

    assert route.claim_standard == "ECLAIMLINK"
    assert route.jurisdiction == "DUBAI"
    assert route.evidence["payer_registry_match"]["canonical_id"] == "oman_insurance_ae"


def test_poll_worker_archives_callback_and_injects_checkpoint():
    container = build_default_container()
    checkpoint_store = MemoryCheckpointStore()
    checkpoint_id = checkpoint_store.put("thread-1", {"claim": {"claim_id": "CLM-CALLBACK-001"}})
    resumed_states = []

    class DonePayerAdapter:
        def get_status(self, job):
            return {"status": "complete", "transaction_ref": "TX-1", "pre_auth_ref": "AUTH-1"}

    result = process_poll_job(
        job={
            "job_id": "job-1",
            "claim_id": "CLM-CALLBACK-001",
            "payer_id": "A001",
            "thread_id": "thread-1",
            "checkpoint_id": checkpoint_id,
            "resume_node": "parse_final_response",
        },
        cache=container.cache,
        repository=container.repository,
        payer_adapter=DonePayerAdapter(),
        checkpoint_store=checkpoint_store,
        object_store=container.object_store,
        resume_callback=resumed_states.append,
    )

    assert result["callback"]["status"] == "accepted"
    assert resumed_states[0]["callback_results"]["parse_final_response"]["pre_auth_ref"] == "AUTH-1"
    assert any("/callbacks/" in uri for uri in container.object_store.objects)
