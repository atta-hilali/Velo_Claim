import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CLAIM_PREP_PATH = ROOT / "Claim Preparation Agent.py"
CLAIM_VALIDATION_PATH = ROOT / "Claim Validation Agent.py"
INPUT_PATH = ROOT / "sample_inputs" / "dubai_clinic_full_pipeline.json"
OUTPUT_PATH = ROOT / "sample_outputs" / "dubai_full_pipeline_output.json"


def load_module(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def dubai_validation_rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "DXB-TF-001",
            "payer_id": "A001",
            "version": "2026.06.17",
            "status": "ACTIVE",
            "rule_type": "TIMELY_FILING",
            "jurisdiction": "DUBAI",
            "effective_from": "2026-01-01",
            "condition": {},
            "action": {"limit_days": 90},
            "severity": "FAIL",
            "message": "Dubai eClaimLink claims should be submitted within 90 days of service.",
            "max_deduction_per_layer": 10.0,
        },
        {
            "rule_id": "DXB-DOC-SOAP-001",
            "payer_id": "A001",
            "version": "2026.06.17",
            "status": "ACTIVE",
            "rule_type": "DOCUMENTATION",
            "jurisdiction": "DUBAI",
            "effective_from": "2026-01-01",
            "condition": {},
            "action": {"required_documents": ["soap_note"]},
            "severity": "FAIL",
            "message": "SOAP note must be available for Dubai outpatient claim validation.",
            "max_deduction_per_layer": 25.0,
        },
    ]


def main() -> None:
    claim_prep = load_module(CLAIM_PREP_PATH, "claim_preparation_agent")
    claim_validation = load_module(CLAIM_VALIDATION_PATH, "claim_validation_agent")
    initial_state = json.loads(INPUT_PATH.read_text(encoding="utf-8"))

    prep_result = claim_prep.run_claim_preparation(initial_state)
    require(prep_result["status"] == "READY_FOR_VALIDATION", prep_result.get("errors"))
    require(prep_result["claim_format"] == "ECLAIMLINK", prep_result["claim_format"])
    require(prep_result["jurisdiction"] == "DUBAI", prep_result["jurisdiction"])
    require(prep_result["claim_payload_type"] == "application/xml", prep_result["claim_payload_type"])
    require("<Claim.Submission>" in prep_result["claim_payload"], "Expected eClaimLink XML payload")
    require("<ReceiverID>A001</ReceiverID>" in prep_result["claim_payload"], "Expected DHA payer code in ReceiverID")
    require("<PayerID>A001</PayerID>" in prep_result["claim_payload"], "Expected DHA payer code in PayerID")
    require("<ProviderID>DHA123456</ProviderID>" in prep_result["claim_payload"], "Expected DHA facility code in ProviderID")
    require("<Code>99213</Code>" in prep_result["claim_payload"], "Expected CPT 99213 in XML payload")
    require("<Type>3</Type>" in prep_result["claim_payload"], "Expected numeric CPT activity type")
    require("<Clinician>DHA98765</Clinician>" in prep_result["claim_payload"], "Expected activity clinician")
    require(
        "<ActivityStart>2026-06-15T10:35:00+04:00</ActivityStart>" in prep_result["claim_payload"],
        "Expected activity date-time",
    )

    validation_result = claim_validation.run_claim_validation(
        {
            "raw_claim": prep_result["claim"],
            "payer_rules": dubai_validation_rules(),
            "submission_date": "2026-06-17",
            "skip_fhir_fetch": True,
        }
    )
    require(validation_result["status"] == "CLAIM_VALIDATION_COMPLETED", validation_result.get("errors"))
    require(validation_result["routing"] == "READY_TO_SUBMIT", validation_result["routing"])

    output = {
        "input_file": str(INPUT_PATH),
        "claim_preparation_summary": {
            "status": prep_result.get("status"),
            "claim_id": prep_result.get("claim_id"),
            "claim_format": prep_result.get("claim_format"),
            "jurisdiction": prep_result.get("jurisdiction"),
            "payload_type": prep_result.get("claim_payload_type"),
            "warnings": prep_result.get("warnings", []),
            "errors": prep_result.get("errors", []),
        },
        "claim_from_preparation_agent": prep_result.get("claim"),
        "claim_payload": prep_result.get("claim_payload"),
        "validation_summary": {
            "status": validation_result.get("status"),
            "score": validation_result.get("score"),
            "routing": validation_result.get("routing"),
            "warnings": validation_result.get("warnings", []),
            "errors": validation_result.get("errors", []),
        },
        "validation_report": validation_result.get("report"),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    print("Dubai full pipeline test passed.")
    print("Input:", INPUT_PATH)
    print("Output:", OUTPUT_PATH)
    print(
        "Claim preparation:",
        {
            "claim_id": prep_result.get("claim_id"),
            "format": prep_result.get("claim_format"),
            "jurisdiction": prep_result.get("jurisdiction"),
            "payload_type": prep_result.get("claim_payload_type"),
        },
    )
    print(
        "Validation:",
        {
            "status": validation_result.get("status"),
            "score": validation_result.get("score"),
            "routing": validation_result.get("routing"),
        },
    )


if __name__ == "__main__":
    main()
