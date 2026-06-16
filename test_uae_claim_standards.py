import copy
import importlib.util
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
AGENT_PATH = ROOT / "Claim Preparation Agent.py"
BASE_INPUT_PATH = ROOT / "sample_inputs" / "nphies_bundle_test.json"
OUTPUT_PATH = ROOT / "sample_outputs" / "uae_claim_standards_test_output.json"


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


def base_state() -> dict[str, Any]:
    return json.loads(BASE_INPUT_PATH.read_text(encoding="utf-8"))


def uae_state(format_name: str) -> dict[str, Any]:
    state = copy.deepcopy(base_state())
    package = state["encounter_package"]
    is_shafafiya = format_name == "SHAFAFIYA"
    jurisdiction = "ABU_DHABI" if is_shafafiya else "DUBAI"
    payer_id = "DAMAN" if is_shafafiya else "DHA-PAYER"
    facility_system = "doh/facility-license" if is_shafafiya else "dha/facility-code"
    clinician_system = "doh/clinician-license" if is_shafafiya else "velo/clinician-license"
    facility_license = "DOH-FAC-001" if is_shafafiya else "DHA-FAC-001"

    state["case_id"] = f"CASE-{format_name}-001"
    state["claim_format"] = format_name
    state["jurisdiction"] = jurisdiction
    state["use_medgemma"] = False
    state["use_knowledge_graph"] = False

    package["encounter"]["id"] = f"ENC-{format_name}-001"
    package["encounter"]["period"]["start"] = "2026-06-11T10:30:00+04:00"
    package["encounter"]["charge_items"][0]["currency"] = "AED"
    package["encounter"]["charge_items"][0]["amount"] = 275.0

    package["patient"]["id"] = f"P-{format_name}-001"
    package["patient"]["identifier"] = [
        {"system": "velo/member-id", "value": f"UAE-M-{format_name}"},
        {"system": "uae/emirates-id", "value": "784-1990-1234567-1"},
    ]
    package["patient"]["birthDate"] = "1990-01-15"
    package["patient"]["gender"] = "male"

    package["coverage"]["id"] = f"COV-{format_name}-001"
    package["coverage"]["subscriberId"] = f"UAE-M-{format_name}"
    package["coverage"]["payor"] = [
        {"identifier": {"value": payer_id}, "display": payer_id}
    ]
    package["coverage"]["period"] = {"start": "2026-01-01", "end": "2026-12-31"}

    package["provider"]["id"] = f"D-{format_name}-001"
    package["provider"]["identifier"] = [
        {"system": clinician_system, "value": f"{format_name}-D-001"}
    ]

    package["facility"]["id"] = f"FAC-{format_name}-001"
    package["facility"]["name"] = f"Velo Clinic {jurisdiction}"
    package["facility"]["identifier"] = [
        {"system": facility_system, "value": facility_license}
    ]
    return state


def assert_common_claim_xml(result: dict[str, Any], expected_format: str, expected_jurisdiction: str) -> ElementTree.Element:
    require(result["status"] == "READY_FOR_VALIDATION", f"Unexpected status: {result['status']}")
    require(result["claim_format"] == expected_format, f"Unexpected format: {result['claim_format']}")
    require(result["jurisdiction"] == expected_jurisdiction, f"Unexpected jurisdiction: {result['jurisdiction']}")
    require(result["claim_payload_type"] == "application/xml", f"{expected_format} payload must be XML")
    require(
        result["formatted_claim"]["schema_status"] == "draft_adapter_not_payer_certified",
        f"{expected_format} adapter should remain draft until payer certification",
    )
    root = ElementTree.fromstring(result["claim_payload"])
    require(root.tag == "Claim.Submission", f"Unexpected XML root for {expected_format}: {root.tag}")
    require(root.findtext("./Header/SenderID"), f"{expected_format} header must include SenderID")
    require(root.findtext("./Header/ReceiverID"), f"{expected_format} header must include ReceiverID")
    require(root.findtext("./Claim/ID") == result["claim_id"], f"{expected_format} XML Claim ID must match result")
    require(root.findtext("./Claim/MemberID"), f"{expected_format} claim must include MemberID")
    require(root.findtext("./Claim/EmiratesIDNumber"), f"{expected_format} claim must include Emirates ID")
    require(root.findtext("./Claim/Encounter/Activity/Code") == "99213", f"{expected_format} activity code missing")
    return root


def main() -> None:
    agent = load_agent_module()

    shafafiya = agent.run_claim_preparation(uae_state("SHAFAFIYA"))
    eclaimlink = agent.run_claim_preparation(uae_state("ECLAIMLINK"))

    shafafiya_xml = assert_common_claim_xml(shafafiya, "SHAFAFIYA", "ABU_DHABI")
    require(
        shafafiya_xml.findtext("./Header/DispositionFlag") == "PRODUCTION_DRAFT",
        "Shafafiya XML should include draft disposition flag",
    )
    require(shafafiya_xml.findtext("./Claim/ClaimNet") == "275.00", "Shafafiya ClaimNet missing")
    require(
        shafafiya_xml.findtext("./Claim/Encounter/Diagnosis/Code") == "J18.9",
        "Shafafiya diagnosis code missing",
    )

    eclaimlink_xml = assert_common_claim_xml(eclaimlink, "ECLAIMLINK", "DUBAI")
    require(
        eclaimlink_xml.find("./Header/DispositionFlag") is None,
        "eClaimLink XML should not use Shafafiya DispositionFlag",
    )
    require(eclaimlink_xml.findtext("./Claim/NetAmount") == "275.00", "eClaimLink NetAmount missing")
    require(
        eclaimlink_xml.findtext("./Claim/Encounter/Diagnosis/DiagnosisCode") == "J18.9",
        "eClaimLink diagnosis code missing",
    )
    require(
        eclaimlink_xml.findtext("./Claim/Encounter/Activity/ActivityStart"),
        "eClaimLink ActivityStart missing",
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "shafafiya": {
                    "claim_id": shafafiya["claim_id"],
                    "format": shafafiya["claim_format"],
                    "jurisdiction": shafafiya["jurisdiction"],
                    "payload_type": shafafiya["claim_payload_type"],
                    "payload": shafafiya["claim_payload"],
                    "warnings": shafafiya.get("warnings", []),
                },
                "eclaimlink": {
                    "claim_id": eclaimlink["claim_id"],
                    "format": eclaimlink["claim_format"],
                    "jurisdiction": eclaimlink["jurisdiction"],
                    "payload_type": eclaimlink["claim_payload_type"],
                    "payload": eclaimlink["claim_payload"],
                    "warnings": eclaimlink.get("warnings", []),
                },
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print("UAE claim standards tests passed.")
    print("Output:", OUTPUT_PATH)
    print("Shafafiya:", {"claim_id": shafafiya["claim_id"], "format": shafafiya["claim_format"]})
    print("eClaimLink:", {"claim_id": eclaimlink["claim_id"], "format": eclaimlink["claim_format"]})


if __name__ == "__main__":
    main()
