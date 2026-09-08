import json

from velo_claim.builders.claim.nphies import (
    DIAGNOSIS_TYPE_SYSTEM,
    NphiesClaimBuilder,
    PATIENT_IDENTIFIER_SYSTEM,
)
from velo_claim.core.enums import ClaimStandard
from velo_claim.validation.payload_validators import PayloadValidationConfig, PayloadValidator


def _claim() -> dict:
    return {
        "claim_id": "CLM-NPHIES-001",
        "patient": {
            "id": "PAT-001",
            "name": "Fatima Al Mansoori",
            "member_id": "MEM-001",
            "national_identifier": "1023456789",
            "birth_date": "1990-01-15",
            "gender": "female",
        },
        "payer": {
            "id": "PAYER-001",
            "name": "Test Insurer",
            "plan_id": "BASIC",
            "coverage_id": "COV-001",
            "coverage_status": "active",
            "coverage_period": {"start": "2026-01-01", "end": "2026-12-31"},
        },
        "provider": {
            "id": "PRAC-001",
            "name": "Sara Al Haddad",
            "license": "SCFHS-001",
            "facility_id": "FAC-001",
            "facility_name": "Velo Clinic Riyadh",
            "facility_license": "NPHIES-FAC-001",
        },
        "encounter": {
            "id": "ENC-001",
            "status": "finished",
            "class_code": "AMB",
            "period": {"start": "2026-06-11T10:30:00+03:00"},
            "service_date": "2026-06-11",
        },
        "diagnoses": [
            {"code": "J18.9", "description": "Pneumonia", "type": "principal"}
        ],
        "line_items": [
            {
                "id": "ACT-001",
                "system": "CPT",
                "code": "99213",
                "description": "Office visit",
                "quantity": 1,
                "net": 275.0,
                "currency": "SAR",
                "service_date": "2026-06-11",
            }
        ],
        "amount": {"net": 275.0, "currency": "SAR"},
        "pre_auth_ref": "AUTH-001",
    }


def _validator() -> PayloadValidator:
    return PayloadValidator(
        PayloadValidationConfig(
            nphies_profile_required=True,
            nphies_fhir_validator_command=None,
        )
    )


def test_nphies_claim_builder_emits_complete_message_bundle():
    bundle = json.loads(NphiesClaimBuilder().build(_claim()))
    resources = [entry["resource"] for entry in bundle["entry"]]

    assert [resource["resourceType"] for resource in resources] == [
        "MessageHeader",
        "Organization",
        "Organization",
        "Practitioner",
        "Patient",
        "Coverage",
        "Encounter",
        "Claim",
    ]
    header, _, _, practitioner, patient, coverage, encounter, claim = resources
    assert header["id"]
    assert header["sender"]["identifier"]["use"] == "official"
    assert practitioner["name"][0]["given"] and practitioner["name"][0]["family"]
    assert patient["identifier"][0]["system"] == PATIENT_IDENTIFIER_SYSTEM
    assert coverage["relationship"]["coding"][0]["code"] == "self"
    assert encounter["period"]["start"] != encounter["period"]["end"]
    assert claim["diagnosis"][0]["type"][0]["coding"][0]["system"] == DIAGNOSIS_TYPE_SYSTEM
    assert claim["insurance"][0]["preAuthRef"] == ["AUTH-001"]


def test_nphies_claim_runtime_identifiers_are_regenerated():
    first = json.loads(NphiesClaimBuilder().build(_claim()))
    second = json.loads(NphiesClaimBuilder().build(_claim()))

    assert first["id"] != second["id"]
    assert first["entry"][0]["resource"]["id"] != second["entry"][0]["resource"]["id"]


def test_local_nphies_validator_rejects_missing_required_resource():
    bundle = json.loads(NphiesClaimBuilder().build(_claim()))
    bundle["entry"] = [
        entry for entry in bundle["entry"] if entry["resource"]["resourceType"] != "Patient"
    ]

    _, result = _validator().validate(
        payload=json.dumps(bundle),
        payload_type="fhir_bundle_json",
        route={"claim_standard": ClaimStandard.NPHIES},
    )

    assert not result.passes
    assert any(issue.code == "NPHIES_RESOURCE_ORDER_INVALID" for issue in result.issues)


def test_local_nphies_validator_accepts_complete_structure_with_advisory_only():
    _, result = _validator().validate(
        payload=NphiesClaimBuilder().build(_claim()),
        payload_type="fhir_bundle_json",
        route={"claim_standard": ClaimStandard.NPHIES},
    )

    assert result.passes
    assert [issue.code for issue in result.issues] == ["FHIR_VALIDATOR_NOT_CONFIGURED"]
