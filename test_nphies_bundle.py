import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
AGENT_PATH = ROOT / "Claim Preparation Agent.py"
INPUT_PATH = ROOT / "sample_inputs" / "nphies_bundle_test.json"
OUTPUT_PATH = ROOT / "sample_outputs" / "nphies_bundle_test_output.json"


def load_agent_module() -> Any:
    spec = importlib.util.spec_from_file_location("claim_preparation_agent", AGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {AGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    agent = load_agent_module()
    input_state = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    result = agent.run_claim_preparation(input_state)

    require(result["status"] == "READY_FOR_VALIDATION", f"Unexpected status: {result['status']}")
    require(result["claim_format"] == "NPHIES", f"Unexpected format: {result['claim_format']}")
    require(result["jurisdiction"] == "KSA", f"Unexpected jurisdiction: {result['jurisdiction']}")
    require(result["claim_payload_type"] == "application/fhir+json", "NPHIES payload must be FHIR JSON")
    require(
        result["formatted_claim"]["schema_status"] == "nphies_sandbox_ready",
        "NPHIES submission schema_status must be nphies_sandbox_ready",
    )

    bundle = result["claim_payload"]
    second_bundle = agent.run_claim_preparation(input_state)["claim_payload"]
    require(bundle["resourceType"] == "Bundle", "NPHIES payload must be a FHIR Bundle")
    require(bundle["type"] == "message", "NPHIES Bundle.type must be message")
    require(uuid.UUID(bundle["id"]).version == 4, "NPHIES Bundle.id must be generated with uuid4")
    require(
        bundle["id"] != second_bundle["id"],
        "NPHIES Bundle.id must be regenerated for every submission",
    )
    require(
        bundle.get("meta", {}).get("profile") == [
            "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/bundle"
        ],
        "Bundle.meta.profile must use the official NPHIES Bundle profile",
    )

    resources = [entry["resource"] for entry in bundle.get("entry", [])]
    resource_types = [resource.get("resourceType") for resource in resources]
    require(resource_types[0] == "MessageHeader", "MessageHeader must be the first Bundle entry")
    for expected in [
        "MessageHeader",
        "Organization",
        "Practitioner",
        "Patient",
        "Coverage",
        "Encounter",
        "Claim",
    ]:
        require(expected in resource_types, f"Missing {expected} entry in NPHIES Bundle")

    message_header = resources[0]
    require(
        message_header.get("meta", {}).get("profile") == [
            "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/message-header"
        ],
        "MessageHeader.meta.profile must use the official NPHIES MessageHeader profile",
    )
    require(
        message_header["eventCoding"]["system"]
        == "http://nphies.sa/terminology/CodeSystem/ksa-message-events",
        "MessageHeader.eventCoding.system must use the official KSA Message Events system",
    )
    require(
        message_header["eventCoding"]["code"] == "claim-request",
        "MessageHeader.eventCoding.code must be claim-request",
    )
    require(
        message_header["destination"][0]["receiver"]["identifier"]["system"]
        == "http://nphies.sa/license/payer-license",
        "MessageHeader destination receiver must include payer license identifier",
    )
    require(
        message_header["source"]["endpoint"] == "https://velodoc.ai/fhir",
        "MessageHeader.source.endpoint must be the Velo Claim FHIR endpoint URL",
    )

    organizations = [resource for resource in resources if resource.get("resourceType") == "Organization"]
    org_profiles = {
        resource["meta"]["profile"][0]
        for resource in organizations
    }
    require(
        "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/provider-organization"
        in org_profiles,
        "Provider Organization resource is missing",
    )
    require(
        "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/insurer-organization"
        in org_profiles,
        "Insurer Organization resource is missing",
    )

    practitioner = next(resource for resource in resources if resource.get("resourceType") == "Practitioner")
    require(
        practitioner.get("meta", {}).get("profile") == [
            "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/practitioner"
        ],
        "Practitioner must use the official NPHIES Practitioner profile",
    )
    require(
        practitioner["identifier"][0]["system"] == "http://nphies.sa/license/practitioner-license",
        "Practitioner identifier must use the NPHIES practitioner license system",
    )

    patient = next(resource for resource in resources if resource.get("resourceType") == "Patient")
    patient_name = patient["name"][0]
    require(patient_name.get("text"), "Patient.name.text must be present")
    require(patient_name.get("family"), "Patient.name.family must be present")
    require(patient_name.get("given"), "Patient.name.given must be present")

    coverage = next(resource for resource in resources if resource.get("resourceType") == "Coverage")
    require(
        coverage["relationship"]["coding"][0]["system"]
        == "http://terminology.hl7.org/CodeSystem/subscriber-relationship",
        "Coverage.relationship must use the subscriber relationship terminology system",
    )
    require(
        coverage["relationship"]["coding"][0]["code"] == "self",
        "Coverage.relationship must indicate self",
    )
    require(
        coverage.get("period") == {"start": "2026-01-01", "end": "2026-12-31"},
        "Coverage.period must be copied from source coverage",
    )

    encounter = next(resource for resource in resources if resource.get("resourceType") == "Encounter")
    require(
        encounter.get("meta", {}).get("profile") == [
            "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/encounter-claim-AMB"
        ],
        "Encounter must use the NPHIES Encounter AMB Claim profile",
    )
    require(
        encounter["participant"][0]["individual"]["reference"] == f"Practitioner/{practitioner['id']}",
        "Encounter must reference the included Practitioner",
    )
    require(
        encounter["period"]["end"] == "2026-06-11T11:00:00+03:00",
        "Encounter.period.end must default to start + 30 minutes",
    )

    claim = next(resource for resource in resources if resource.get("resourceType") == "Claim")
    require(claim["id"] == result["claim_id"], "Claim resource id must match result claim_id")
    require(
        claim.get("meta", {}).get("profile") == [
            "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/professional-claim"
        ],
        "Claim.meta.profile must use the official NPHIES Professional Claim profile",
    )
    require(claim["status"] == "active", "Claim status must be active")
    require(claim["use"] == "claim", "Claim.use must be claim")
    require(
        claim["type"]["coding"][0]["system"] == "http://terminology.hl7.org/CodeSystem/claim-type",
        "Claim.type must use the HL7 claim-type CodeSystem",
    )
    require(
        claim["type"]["coding"][0]["code"] == "professional",
        "Claim.type code must be professional",
    )
    require(
        claim["priority"]["coding"][0]["system"]
        == "http://terminology.hl7.org/CodeSystem/processpriority",
        "Claim.priority must use the HL7 processpriority CodeSystem",
    )
    require(
        claim["priority"]["coding"][0]["code"] == "normal",
        "Claim.priority code must be normal",
    )
    require(claim.get("total", {}).get("currency") == "SAR", "NPHIES total currency should be SAR")
    require(claim.get("diagnosis"), "Claim must include diagnosis")
    require(
        claim["diagnosis"][0]["type"][0]["coding"][0]["system"]
        == "http://nphies.sa/terminology/CodeSystem/diagnosis-type",
        "Claim.diagnosis.type must use the NPHIES diagnosis-type CodeSystem",
    )
    require(
        claim["diagnosis"][0]["type"][0]["coding"][0]["code"] == "principal",
        "Claim.diagnosis.type code must be principal",
    )
    require(claim.get("procedure"), "Claim must include procedure")
    require(claim.get("item"), "Claim must include item lines")
    require(
        any(
            extension.get("url")
            == "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/extension-encounter"
            and extension.get("valueReference", {}).get("reference") == f"Encounter/{encounter['id']}"
            for extension in claim.get("extension", [])
        ),
        "Claim must reference the included Encounter resource",
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    print("NPHIES Bundle structure test passed.")
    print("Output:", OUTPUT_PATH)
    print("Bundle id:", bundle["id"])
    print("Entries:", resource_types)
    print("Claim id:", claim["id"])
    print("Total:", claim["total"])


if __name__ == "__main__":
    main()
