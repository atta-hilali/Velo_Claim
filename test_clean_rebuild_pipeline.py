import json

from velo_claim.builders.eligibility.nphies import NphiesEligibilityBuilder
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.builders.claim.nphies import NphiesClaimBuilder
from velo_claim.builders.prior_auth.canonical import PACanonicalForm
from velo_claim.builders.prior_auth.nphies import NphiesPABuilder
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.core.container import build_default_container
from velo_claim.core.enums import EligibilityStatus, PayloadStatus, Severity
from velo_claim.core.models import RoutingContext
from velo_claim.checks.eligibility_graph import run_eligibility_subgraph
from velo_claim.checks.prior_auth_graph import run_prior_auth_subgraph
from velo_claim.examples.demo_inputs import abu_dhabi_pneumonia_encounter
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.jobs.poll_worker import process_poll_job
from velo_claim.pipeline import run_full_pipeline
from velo_claim.routing.router import ClaimRouter
from velo_claim.validation.payload_validators import PayloadValidator


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


def test_eligibility_subgraph_enters_waiting_for_payer():
    container = build_default_container()
    payer_rules = container.payer_rule_loader.load("A001", "TH4QF")
    checkpoint_store = MemoryCheckpointStore()

    state, result = run_eligibility_subgraph(
        state={
            **_eligibility_state(),
            "thread_id": "eligibility-thread-1",
            "eligibility_submit_to_payer": True,
        },
        payer_rules=payer_rules,
        cache=container.cache,
        repository=container.repository,
        object_store=container.object_store,
        checkpoint_store=checkpoint_store,
    )

    assert result.status == EligibilityStatus.WAITING_FOR_PAYER
    assert state["payload_status"] == PayloadStatus.WAITING_FOR_PAYER
    assert state["callback_state"]["resume_node"] == "parse_eligibility_response"
    assert container.cache.keys("bg_job:")


def test_eligibility_subgraph_parses_callback_response():
    container = build_default_container()
    payer_rules = container.payer_rule_loader.load("A001", "TH4QF")

    state, result = run_eligibility_subgraph(
        state={
            **_eligibility_state(),
            "callback_results": {
                "parse_eligibility_response": {
                    "status": "complete",
                    "eligibility_ref": "ELIG-AUH-001",
                    "benefit_summary": {"copay": 0},
                }
            },
        },
        payer_rules=payer_rules,
        cache=container.cache,
        repository=container.repository,
        object_store=container.object_store,
    )

    assert result.status == EligibilityStatus.PASS
    assert state["canonical_claim"]["payer"]["eligibility_ref"] == "ELIG-AUH-001"
    assert state["canonical_claim"]["payer"]["benefit_summary"] == {"copay": 0}


def test_nphies_eligibility_builder_matches_message_structure():
    payload = NphiesEligibilityBuilder().build(_nphies_canonical_claim(), {})
    bundle = json.loads(payload)
    resources = [entry["resource"] for entry in bundle["entry"]]
    resource_types = [resource["resourceType"] for resource in resources]

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "message"
    assert resources[0]["resourceType"] == "MessageHeader"
    assert resources[0]["eventCoding"]["code"] == "eligibility-request"
    assert "CoverageEligibilityRequest" in resource_types
    assert "Coverage" in resource_types
    assert "Patient" in resource_types
    assert resource_types.count("Organization") >= 2

    _, result = PayloadValidator().validate_eligibility_request(
        payload=payload,
        payload_type="fhir_bundle_json",
        route={"claim_standard": "NPHIES"},
    )

    assert not any(issue.severity in {Severity.CRITICAL, Severity.ERROR} for issue in result.issues)


def test_nphies_prior_auth_builder_matches_message_structure():
    payload = NphiesPABuilder().build(_nphies_pa_form())
    bundle = json.loads(payload)
    resources = [entry["resource"] for entry in bundle["entry"]]
    resource_types = [resource["resourceType"] for resource in resources]

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "message"
    assert resources[0]["resourceType"] == "MessageHeader"
    assert resources[0]["eventCoding"]["code"] == "priorauth-request"
    assert "Claim" in resource_types
    assert "Coverage" in resource_types
    assert "Patient" in resource_types
    assert resource_types.count("Organization") >= 2
    assert "Practitioner" in resource_types
    assert "Encounter" in resource_types
    assert next(resource for resource in resources if resource["resourceType"] == "Claim")["use"] == "preauthorization"

    _, result = PayloadValidator().validate_prior_request(
        payload=payload,
        payload_type="fhir_bundle_json",
        route={"prior_auth_standard": "NPHIES"},
    )

    assert not any(issue.severity in {Severity.CRITICAL, Severity.ERROR} for issue in result.issues)


def test_eligibility_subgraph_builds_nphies_payload_when_waiting():
    container = build_default_container()
    payer_rules = container.payer_rule_loader.load("B002", "PLN01")

    state, result = run_eligibility_subgraph(
        state={
            "route": {"claim_standard": "NPHIES", "eligibility_profile": "KSA_STANDARD"},
            "routing_context": {"payer_id": "B002", "plan_id": "PLN01"},
            "canonical_claim": _nphies_canonical_claim(),
            "source_context": {},
            "payload_status": PayloadStatus.DRAFT_BUILT,
            "eligibility_submit_to_payer": True,
        },
        payer_rules=payer_rules,
        cache=container.cache,
        repository=container.repository,
        object_store=container.object_store,
    )

    payload = json.loads(result.data["payload"])
    assert result.status == EligibilityStatus.WAITING_FOR_PAYER
    assert payload["entry"][0]["resource"]["eventCoding"]["code"] == "eligibility-request"
    assert state["eligibility_payload_type"] == "fhir_bundle_json"
    assert state["eligibility_payload_uri"]


def test_prior_auth_callback_approval_is_persisted_and_forces_rebuild():
    container = build_default_container()
    payer_rules = container.payer_rule_loader.load("A001", "TH4QF")
    pa_builder = PAClaimBuilderModule(repository=container.repository, object_store=container.object_store)
    state = _shafafiya_pa_state(
        callback_response={
            "status": "complete",
            "decision": "approved",
            "pre_auth_ref": "AUTH-AUH-70553",
            "cpt_codes": ["70553"],
            "valid_from": "2026-06-01",
            "valid_to": "2026-07-01",
            "transaction_ref": "TX-AUH-PA-001",
        }
    )

    result_state, result = run_prior_auth_subgraph(
        state=state,
        payer_rules=payer_rules,
        kg_client=container.kg_client,
        repository=container.repository,
        pa_builder=pa_builder,
        object_store=container.object_store,
    )

    assert result.status == "APPROVED"
    assert result_state["canonical_claim"]["pre_auth_ref"] == "AUTH-AUH-70553"
    assert result_state["payload_rebuild_required"] is True
    assert container.repository.find_prior_auth_response("CLM-PA-AUH-001", "A001", "70553")["pre_auth_ref"] == "AUTH-AUH-70553"

    claim_builder = ClaimBuilderModule(
        repository=container.repository,
        object_store=container.object_store,
        kg_client=container.kg_client,
        payer_rule_loader=container.payer_rule_loader,
    )
    rebuilt = claim_builder.build(result_state)

    assert "<PriorAuthorizationID>AUTH-AUH-70553</PriorAuthorizationID>" in rebuilt["claim_payload"]
    assert rebuilt["canonical_claim"]["pre_auth_ref"] == "AUTH-AUH-70553"
    assert rebuilt["payload_rebuild_required"] is False


def test_nphies_claim_builder_attaches_prior_auth_reference():
    claim = {**_nphies_canonical_claim(), "pre_auth_ref": "NPHIES-AUTH-27447"}
    claim["line_items"] = [{**claim["line_items"][0], "code": "27447"}]
    claim["amount"] = {"net": 2500.0, "currency": "SAR"}
    payload = NphiesClaimBuilder().build(claim)
    bundle = json.loads(payload)
    claim_resource = next(entry["resource"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Claim")

    assert claim_resource["insurance"][0]["preAuthRef"] == ["NPHIES-AUTH-27447"]


def _eligibility_state():
    return {
        "route": {"claim_standard": "SHAFAFIYA", "eligibility_profile": "DAMAN_VOI"},
        "routing_context": {"payer_id": "A001", "plan_id": "TH4QF"},
        "canonical_claim": {
            "claim_id": "CLM-ELIG-001",
            "patient": {"id": "PAT-ELIG-001"},
            "payer": {
                "id": "A001",
                "plan_id": "TH4QF",
                "coverage_status": "active",
                "coverage_period": {"start": "2026-01-01", "end": "2026-12-31"},
            },
            "encounter": {"service_date": "2026-06-16"},
            "line_items": [{"code": "99213"}],
        },
        "source_context": {"coverage": {"voi_verified": True}},
        "payload_status": PayloadStatus.DRAFT_BUILT,
    }


def _nphies_canonical_claim():
    return {
        "claim_id": "CLM-NPHIES-ELIG-001",
        "patient": {
            "id": "PAT-NPHIES-001",
            "name": "Derrick Lin",
            "member_id": "MEM-NPHIES-001",
            "birth_date": "1985-01-02",
            "gender": "male",
        },
        "payer": {
            "id": "B002",
            "name": "BUPA Arabia",
            "plan_id": "PLN01",
            "coverage_id": "COV-NPHIES-001",
            "coverage_status": "active",
            "coverage_period": {"start": "2026-01-01", "end": "2026-12-31"},
        },
        "provider": {
            "facility_id": "FAC-NPHIES-001",
            "facility_name": "Velo Clinic Riyadh",
            "facility_license": "KSA-FAC-001",
        },
        "encounter": {"service_date": "2026-06-20"},
        "line_items": [
            {
                "id": "ACT-NPHIES-001",
                "system": "CPT",
                "code": "99213",
                "description": "Office outpatient visit",
            }
        ],
    }


def _nphies_pa_form():
    claim = _nphies_canonical_claim()
    return PACanonicalForm(
        claim_id=claim["claim_id"],
        patient=claim["patient"],
        coverage=claim["payer"],
        provider={"id": "DR-NPHIES-001", "name": "Dr Sara Haddad", "license": "KSA-DR-001"},
        facility={
            "id": claim["provider"]["facility_id"],
            "name": claim["provider"]["facility_name"],
            "license": claim["provider"]["facility_license"],
        },
        service_date=claim["encounter"]["service_date"],
        procedures=claim["line_items"],
        diagnoses=["J18.9"],
        payer_id=claim["payer"]["id"],
        plan_id=claim["payer"]["plan_id"],
        currency="SAR",
    )


def _shafafiya_pa_state(callback_response=None):
    canonical_claim = {
        "claim_id": "CLM-PA-AUH-001",
        "patient": {
            "id": "PAT-AUH-001",
            "name": "Mariam Ahmed",
            "member_id": "MEM-AUH-001",
            "emirates_id": "784-1990-1234567-1",
        },
        "payer": {
            "id": "A001",
            "name": "DAMAN",
            "plan_id": "TH4QF",
            "coverage_id": "COV-AUH-001",
            "coverage_status": "active",
            "coverage_period": {"start": "2026-01-01", "end": "2026-12-31"},
        },
        "provider": {
            "id": "DR-AUH-001",
            "license": "DHA-DOC-001",
            "facility_id": "FAC-AUH-001",
            "facility_license": "MF2057",
            "facility_name": "Velo Clinic Abu Dhabi",
        },
        "encounter": {
            "id": "ENC-AUH-PA-001",
            "service_date": "2026-06-20",
            "period": {"start": "2026-06-20T10:00:00+04:00", "end": "2026-06-20T10:30:00+04:00"},
            "patient_id": "PAT-AUH-001",
        },
        "diagnoses": [{"system": "ICD-10", "code": "R51", "description": "Headache", "type": "principal"}],
        "procedures": [{"system": "CPT", "code": "70553", "description": "MRI brain", "quantity": 1}],
        "line_items": [
            {
                "id": "ACT-AUH-001",
                "system": "CPT",
                "code": "70553",
                "description": "MRI brain",
                "quantity": 1,
                "gross": 1200.0,
                "patient_share": 0.0,
                "net": 1200.0,
                "currency": "AED",
            }
        ],
        "attachments": [{"name": "radiology_referral.pdf", "type": "RADIOLOGY_REFERRAL"}],
        "amount": {"gross": 1200.0, "patient_share": 0.0, "net": 1200.0, "currency": "AED"},
        "pre_auth_ref": None,
    }
    state = {
        "claim": {"claim_id": canonical_claim["claim_id"]},
        "route": {"claim_standard": "SHAFAFIYA", "prior_auth_standard": "SHAFAFIYA", "jurisdiction": "ABU_DHABI"},
        "routing_context": {"payer_id": "A001", "payer_name": "DAMAN", "plan_id": "TH4QF", "currency": "AED"},
        "canonical_claim": canonical_claim,
        "claim_payload": "<Claim.Submission><Claim><Activity><Code>70553</Code></Activity></Claim></Claim.Submission>",
        "claim_payload_type": "application/xml",
        "source_context": {
            "patient": {
                "id": "PAT-AUH-001",
                "identifier": [{"system": "uae/emirates-id", "value": "784-1990-1234567-1"}],
                "name": [{"text": "Mariam Ahmed"}],
            },
            "coverage": {
                "id": "COV-AUH-001",
                "subscriberId": "MEM-AUH-001",
                "status": "active",
                "period": {"start": "2026-01-01", "end": "2026-12-31"},
            },
            "encounter": canonical_claim["encounter"],
            "provider": {"id": "DR-AUH-001", "name": [{"text": "Dr Sara Haddad"}]},
            "facility": {"id": "FAC-AUH-001", "name": "Velo Clinic Abu Dhabi"},
            "conditions": [{"code": {"coding": [{"code": "R51", "display": "Headache"}]}}],
            "procedures": [{"code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "70553", "display": "MRI brain"}]}, "quantity": 1}],
            "attachments": canonical_claim["attachments"],
            "charge_items": canonical_claim["line_items"],
        },
    }
    if callback_response:
        state["callback_results"] = {"parse_final_response": callback_response}
    return state
