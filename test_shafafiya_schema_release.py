from pathlib import Path

import lxml.etree as LET

from velo_claim.builders.claim.shafafiya import ShafafiyaClaimBuilder
from velo_claim.builders.eligibility.shafafiya import ShafafiyaEligibilityBuilder
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.checks.eligibility_graph import run_eligibility_subgraph
from velo_claim.checks.prior_auth_graph import normalize_prior_auth_response, run_prior_auth_subgraph
from velo_claim.core.container import build_default_container
from velo_claim.core.enums import EligibilityStatus, PriorAuthStatus
from velo_claim.validation.payload_validators import PayloadValidator


SCHEMA_DIR = Path(__file__).parent / "data" / "schemas" / "shafafiya" / "v2.0"


def test_complete_shafafiya_release_compiles():
    for name in (
        "ClaimSubmission.xsd",
        "CommonTypes.xsd",
        "DataDictionary.xsd",
        "PersonRegister.xsd",
        "PriorAuthorization.xsd",
        "PriorRequest.xsd",
        "RemittanceAdvice.xsd",
    ):
        LET.XMLSchema(LET.parse(str(SCHEMA_DIR / name)))


def test_claim_is_xsd_valid_and_embeds_eligibility_and_pa_references():
    claim = _claim()
    claim["payer"]["eligibility_ref"] = "ELIG-A001-001"
    claim["pre_auth_ref"] = "AUTH-A001-001"
    claim["line_items"][0]["system"] = "HCPCS"

    payload = ShafafiyaClaimBuilder().build(claim)
    _, result = PayloadValidator().validate(
        payload=payload,
        payload_type="application/xml",
        route={"claim_standard": "SHAFAFIYA"},
    )

    assert result.passes
    assert "<EligibilityIDPayer>ELIG-A001-001</EligibilityIDPayer>" in payload
    assert "<PriorAuthorizationID>AUTH-A001-001</PriorAuthorizationID>" in payload
    assert "<Type>4</Type>" in payload


def test_pa_builder_preserves_financials_and_creates_one_request():
    container = build_default_container()
    claim = _claim()
    container.repository.upsert_claim(claim["claim_id"], {"status": "DRAFT_BUILT"})
    builder = PAClaimBuilderModule(
        repository=container.repository,
        object_store=container.object_store,
    )

    state = builder.build(_state(claim), ["70553"])

    assert len(container.repository.prior_auth_requests) == 1
    assert state["pa_request_id"] in container.repository.prior_auth_requests
    root = LET.fromstring(state["pa_payload"].encode("utf-8"))
    wire_id = root.findtext("Authorization/ID")
    assert wire_id.startswith("MF2057-PA-")
    assert len(wire_id) <= 30
    assert "<Start>20/06/2026 10:00</Start>" in state["pa_payload"]
    assert "<Net>1200.00</Net>" in state["pa_payload"]
    assert state["pa_payload_validation"]["passes"] is True


def test_shafafiya_pa_response_is_normalized_persisted_and_forces_rebuild():
    container = build_default_container()
    claim = _claim()
    container.repository.upsert_claim(claim["claim_id"], {"status": "DRAFT_BUILT"})
    state = _state(claim)
    state["callback_results"] = {"parse_final_response": {"payload": _authorization_response("Yes")}}
    pa_builder = PAClaimBuilderModule(
        repository=container.repository,
        object_store=container.object_store,
    )

    result_state, result = run_prior_auth_subgraph(
        state=state,
        payer_rules=container.payer_rule_loader.load("A001", "TH4QF"),
        kg_client=container.kg_client,
        repository=container.repository,
        pa_builder=pa_builder,
        object_store=container.object_store,
    )

    assert result.status == PriorAuthStatus.APPROVED
    assert len(container.repository.prior_auth_requests) == 1
    assert result_state["prior_auth_response"]["payer_id"] == "A001"
    assert result_state["prior_auth_response"]["claim_id"] == claim["claim_id"]
    assert result_state["canonical_claim"]["pre_auth_ref"] == "AUTH-A001-001"
    assert result_state["payload_rebuild_required"] is True


def test_shafafiya_eligibility_request_and_response_use_official_transactions():
    container = build_default_container()
    claim = _claim()
    state = _state(claim)
    state["eligibility_submit_to_payer"] = True

    waiting_state, waiting_result = run_eligibility_subgraph(
        state=state,
        payer_rules=container.payer_rule_loader.load("A001", "TH4QF"),
        cache=container.cache,
        repository=container.repository,
        object_store=container.object_store,
    )

    assert waiting_result.status == EligibilityStatus.WAITING_FOR_PAYER
    assert "<Type>Eligibility</Type>" in waiting_state["eligibility_payload"]
    assert waiting_state["eligibility_payload_validation"]["passes"] is True

    callback_state = _state(claim)
    callback_state["callback_results"] = {
        "parse_eligibility_response": {"payload": _authorization_response("Yes", "ELIG-A001-001")}
    }
    final_state, final_result = run_eligibility_subgraph(
        state=callback_state,
        payer_rules=container.payer_rule_loader.load("A001", "TH4QF"),
        cache=container.cache,
        repository=container.repository,
        object_store=container.object_store,
    )

    assert final_result.status == EligibilityStatus.PASS
    assert final_state["canonical_claim"]["payer"]["eligibility_ref"] == "ELIG-A001-001"
    assert final_state["payload_rebuild_required"] is True


def test_shafafiya_parser_does_not_confuse_provider_and_payer_ids():
    parsed = normalize_prior_auth_response(
        {"payload": _authorization_response("Yes")},
        _state(_claim()),
    )

    assert parsed["status"] == "approved"
    assert parsed["payer_id"] == "A001"
    assert parsed["provider_id"] == "MF2057"
    assert parsed["claim_id"] == "CLM-SHAF-001"
    assert parsed["authorization_request_id"] == "MF2057-PA-CLM-SHAF-001"
    assert parsed["pre_auth_ref"] == "AUTH-A001-001"


def _state(claim):
    return {
        "claim": {"claim_id": claim["claim_id"]},
        "route": {
            "claim_standard": "SHAFAFIYA",
            "prior_auth_standard": "SHAFAFIYA",
            "eligibility_profile": "DAMAN_VOI",
            "jurisdiction": "ABU_DHABI",
        },
        "routing_context": {"payer_id": "A001", "plan_id": "TH4QF"},
        "canonical_claim": claim,
        "source_context": {"coverage": {"voi_verified": True}},
        "claim_payload": "<Claim.Submission/>",
        "claim_payload_type": "application/xml",
    }


def _claim():
    return {
        "claim_id": "CLM-SHAF-001",
        "patient": {
            "id": "PAT-AUH-001",
            "member_id": "MEM-AUH-001",
            "emirates_id": "784-1990-1234567-1",
        },
        "payer": {
            "id": "A001",
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
        },
        "encounter": {
            "id": "ENC-AUH-001",
            "patient_id": "PAT-AUH-001",
            "service_date": "2026-06-20",
            "class_code": "AMB",
            "period": {
                "start": "2026-06-20T10:00:00+04:00",
                "end": "2026-06-20T10:30:00+04:00",
            },
        },
        "diagnoses": [{"code": "R51", "type": "principal"}],
        "procedures": [{"system": "CPT", "code": "70553", "quantity": 1}],
        "line_items": [
            {
                "id": "ACT-AUH-001",
                "system": "CPT",
                "code": "70553",
                "quantity": 1,
                "gross": 1200.0,
                "patient_share": 0.0,
                "net": 1200.0,
                "service_date": "2026-06-20T10:00:00+04:00",
            }
        ],
        "amount": {"gross": 1200.0, "patient_share": 0.0, "net": 1200.0, "currency": "AED"},
        "attachments": [],
        "pre_auth_ref": None,
    }


def _authorization_response(result: str, payer_reference: str = "AUTH-A001-001") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Prior.Authorization>
  <Header>
    <SenderID>A001</SenderID>
    <ReceiverID>MF2057</ReceiverID>
    <TransactionDate>20/06/2026 10:01</TransactionDate>
    <RecordCount>1</RecordCount>
    <DispositionFlag>PTE_RESPONSE</DispositionFlag>
  </Header>
  <Authorization>
    <Result>{result}</Result>
    <ID>MF2057-PA-CLM-SHAF-001</ID>
    <IDPayer>{payer_reference}</IDPayer>
    <Start>01/06/2026 00:00</Start>
    <End>01/07/2026 23:59</End>
    <Activity>
      <ID>ACT-AUH-001</ID>
      <Type>3</Type>
      <Code>70553</Code>
      <Quantity>1</Quantity>
      <Net>1200.00</Net>
      <PaymentAmount>1200.00</PaymentAmount>
    </Activity>
  </Authorization>
</Prior.Authorization>"""
