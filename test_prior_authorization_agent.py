import importlib.util
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
AGENT_PATH = ROOT / "Prior Authorization Agent.py"
OUTPUT_PATH = ROOT / "sample_outputs" / "prior_authorization_test_output.json"


def load_agent_module() -> Any:
    spec = importlib.util.spec_from_file_location("prior_authorization_agent", AGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {AGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def base_claim() -> dict[str, Any]:
    return {
        "claim_id": "CLM-PA-BASE",
        "patient": {
            "id": "P-PA-001",
            "name": "Fatima Al-Mansoori",
            "member_id": "M-88921",
            "date_of_birth": "1988-04-19",
            "gender": "female",
        },
        "payer": {"id": "tawuniya_sa", "name": "Tawuniya", "coverage_id": "COV-PA-001"},
        "provider": {"id": "D-PA-001", "facility_id": "FAC-PA-001", "specialty": "Orthopaedics"},
        "encounter_id": "ENC-PA-001",
        "service_date": "2026-06-10",
        "claim_format": "NPHIES",
        "jurisdiction": "KSA",
        "diagnoses": [{"system": "ICD-10", "code": "M17.11", "description": "Right knee osteoarthritis"}],
        "procedures": [{"system": "CPT", "code": "27447", "description": "Total knee arthroplasty"}],
        "line_items": [{"code": "27447", "description": "Total knee arthroplasty", "quantity": 1}],
        "amount": {"net": 45000.0, "currency": "SAR"},
        "attachments": [{"type": "operative_report", "name": "operative_report.pdf"}],
        "prior_auth": {"approved": False, "status": "missing"},
    }


def approved_dry_run_state() -> dict[str, Any]:
    return {
        "raw_claim": base_claim(),
        "dry_run": True,
        "auto_submit": True,
        "simulated_response_status": "approved",
    }


def existing_valid_state() -> dict[str, Any]:
    claim = base_claim()
    claim["prior_auth"] = {
        "ref": "PA-EXISTING-001",
        "approved": True,
        "status": "approved",
        "procedure_codes": ["27447"],
        "valid_from": "2026-01-01",
        "expires": "2026-12-31",
    }
    return {"raw_claim": claim}


def not_required_state() -> dict[str, Any]:
    claim = base_claim()
    claim["claim_id"] = "CLM-PA-NOT-REQUIRED"
    claim["payer"] = {"id": "default", "name": "Default Payer", "coverage_id": "COV-PA-002"}
    claim["procedures"] = [{"system": "CPT", "code": "99213", "description": "Office visit"}]
    claim["line_items"] = [{"code": "99213", "description": "Office visit", "quantity": 1}]
    claim["prior_auth"] = None
    return {"raw_claim": claim, "payer_rules": []}


def missing_documents_state() -> dict[str, Any]:
    claim = base_claim()
    claim["claim_id"] = "CLM-PA-MISSING-DOCS"
    claim["procedures"] = [{"system": "CPT", "code": "73721", "description": "MRI joint without contrast"}]
    claim["line_items"] = [{"code": "73721", "description": "MRI joint without contrast", "quantity": 1}]
    claim["attachments"] = []
    return {
        "raw_claim": claim,
        "payer_rules": [
            {
                "rule_id": "PA-MRI-DOC-001",
                "payer_id": "tawuniya_sa",
                "version": "test",
                "status": "ACTIVE",
                "rule_type": "PRIOR_AUTHORIZATION",
                "jurisdiction": "KSA",
                "condition": {"field": "procedure_codes", "value": ["73721"]},
                "action": {"required_documents": ["clinical_indication_letter", "radiology_referral"]},
                "severity": "FAIL",
                "message": "MRI requires prior authorization and clinical indication documents.",
            }
        ],
    }


def pended_nphies_state() -> dict[str, Any]:
    return {
        "raw_claim": base_claim(),
        "dry_run": True,
        "auto_submit": True,
        "simulated_response_status": "pended",
    }


def queued_then_complete_approved_state() -> dict[str, Any]:
    return {
        "raw_claim": base_claim(),
        "dry_run": True,
        "auto_submit": True,
        "poll_enabled": True,
        "simulated_response_status": "queued",
        "simulated_final_response_status": "complete",
        "simulated_final_decision": "approved",
    }


def partial_state() -> dict[str, Any]:
    return {
        "raw_claim": base_claim(),
        "dry_run": True,
        "auto_submit": True,
        "simulated_response_status": "partial",
    }


def uae_prior_auth_state(
    *,
    claim_format: str,
    jurisdiction: str,
    payer_id: str,
    procedure_code: str,
) -> dict[str, Any]:
    claim = {
        **base_claim(),
        "claim_id": f"CLM-PA-{claim_format}",
        "payer": {"id": payer_id, "name": payer_id.replace("_", " ").title(), "coverage_id": "COV-UAE-001"},
        "provider": {
            "id": "D-UAE-001",
            "facility_id": "FAC-UAE-001",
            "facility_license_number": "UAE-FAC-001",
            "specialty": "Physiotherapy",
        },
        "claim_format": claim_format,
        "jurisdiction": jurisdiction,
        "diagnoses": [{"system": "ICD-10", "code": "M54.5", "description": "Low back pain"}],
        "procedures": [{"system": "CPT", "code": procedure_code, "description": "Therapeutic procedure"}],
        "line_items": [{"code": procedure_code, "description": "Therapeutic procedure", "quantity": 8}],
        "amount": {"net": 900.0, "currency": "AED"},
        "attachments": [{"type": "clinical_indication_letter", "name": "clinical_indication_letter.pdf"}],
        "prior_auth": None,
    }
    return {
        "raw_claim": claim,
        "payer_rules": [
            {
                "rule_id": f"{claim_format}-PA-001",
                "payer_id": payer_id,
                "version": "test",
                "status": "ACTIVE",
                "rule_type": "PRIOR_AUTHORIZATION",
                "jurisdiction": jurisdiction,
                "condition": {"field": "procedure_codes", "value": [procedure_code]},
                "action": {"required_documents": ["clinical_indication_letter"]},
                "severity": "FAIL",
                "message": f"{claim_format} test procedure requires prior authorization.",
            }
        ],
        "dry_run": True,
        "auto_submit": True,
        "simulated_response_status": "approved",
    }


def shafafiya_prior_auth_state() -> dict[str, Any]:
    return uae_prior_auth_state(
        claim_format="SHAFAFIYA",
        jurisdiction="ABU_DHABI",
        payer_id="daman_ae",
        procedure_code="97110",
    )


def eclaimlink_prior_auth_state() -> dict[str, Any]:
    return uae_prior_auth_state(
        claim_format="ECLAIMLINK",
        jurisdiction="DUBAI",
        payer_id="dubai_payer",
        procedure_code="97140",
    )


def assert_prior_auth_xml(result: dict[str, Any], expected_format: str, expected_jurisdiction: str) -> None:
    require(result["routing"] == "APPROVED", f"{expected_format} PA should be approved in dry-run")
    require(result["request_format"] == expected_format, f"Unexpected PA format: {result['request_format']}")
    require(result["jurisdiction"] == expected_jurisdiction, f"Unexpected PA jurisdiction: {result['jurisdiction']}")
    require(result["request_payload_type"] == "application/xml", f"{expected_format} PA payload must be XML")
    root = ElementTree.fromstring(result["request_payload"])
    require(root.tag == "Prior.Authorization.Request", f"Unexpected {expected_format} PA XML root: {root.tag}")
    require(root.findtext("./Authorization/ID"), f"{expected_format} PA XML should include authorization ID")
    require(root.findtext("./Authorization/PayerID"), f"{expected_format} PA XML should include payer ID")
    require(root.findtext("./Authorization/Activities/Activity/Code"), f"{expected_format} PA XML should include activity code")
    require(
        any(warning["type"] == "DRAFT_PRIOR_AUTH_ADAPTER" for warning in result.get("warnings", [])),
        f"{expected_format} PA should warn that the adapter is draft",
    )


def main() -> None:
    agent = load_agent_module()

    approved = agent.run_prior_authorization(approved_dry_run_state())
    existing = agent.run_prior_authorization(existing_valid_state())
    not_required = agent.run_prior_authorization(not_required_state())
    missing_docs = agent.run_prior_authorization(missing_documents_state())
    pended = agent.run_prior_authorization(pended_nphies_state())
    queued_complete = agent.run_prior_authorization(queued_then_complete_approved_state())
    partial = agent.run_prior_authorization(partial_state())
    shafafiya = agent.run_prior_authorization(shafafiya_prior_auth_state())
    eclaimlink = agent.run_prior_authorization(eclaimlink_prior_auth_state())

    require(approved["routing"] == "APPROVED", f"Unexpected approved routing: {approved['routing']}")
    require(approved["status"] == "PRIOR_AUTH_APPROVED", f"Unexpected approved status: {approved['status']}")
    require(approved["pre_auth_ref"].startswith("PA-"), "Approved dry-run should create a PA reference")
    require(
        approved["updated_claim"]["prior_auth"]["approved"] is True,
        "Approved dry-run should update claim.prior_auth",
    )

    require(existing["routing"] == "ALREADY_VALID", f"Unexpected existing routing: {existing['routing']}")
    require(
        existing["updated_claim"]["prior_auth"]["ref"] == "PA-EXISTING-001",
        "Existing valid PA should be preserved",
    )

    require(not_required["routing"] == "NOT_REQUIRED", f"Unexpected no-PA routing: {not_required['routing']}")
    require(not_required["requirements"] == [], "No-PA state should not include requirements")

    require(missing_docs["routing"] == "NEEDS_DOCUMENTS", f"Unexpected docs routing: {missing_docs['routing']}")
    require(
        set(missing_docs["missing_documents"]) == {"clinical_indication_letter", "radiology_referral"},
        f"Unexpected missing docs: {missing_docs['missing_documents']}",
    )
    require(pended["routing"] == "PENDED_NPHIES", f"Unexpected pended routing: {pended['routing']}")
    require(pended["nphies_generated"] is True, "Pended NPHIES response should expose nphies-generated")
    require(
        pended["report"]["alternate_follow_up_required"] is True,
        "Pended NPHIES response should require alternate follow-up",
    )

    require(
        queued_complete["routing"] == "APPROVED",
        f"Unexpected queued-complete routing: {queued_complete['routing']}",
    )
    require(
        queued_complete["report"]["response_outcome"] == "complete",
        f"Unexpected queued-complete outcome: {queued_complete['report']['response_outcome']}",
    )
    require(
        queued_complete["report"]["adjudication_mode"] == "non_real_time",
        "Queued then complete flow should remain non-real-time because it was polled",
    )
    require(
        queued_complete["pre_auth_ref"].startswith("PA-"),
        "Queued then complete approved flow should store preAuthRef",
    )

    require(partial["routing"] == "PARTIAL", f"Unexpected partial routing: {partial['routing']}")
    require(partial["report"]["polling_required"] is True, "Partial response should require polling")
    assert_prior_auth_xml(shafafiya, "SHAFAFIYA", "ABU_DHABI")
    assert_prior_auth_xml(eclaimlink, "ECLAIMLINK", "DUBAI")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "approved": approved["report"],
                "existing_valid": existing["report"],
                "not_required": not_required["report"],
                "missing_documents": missing_docs["report"],
                "pended_nphies": pended["report"],
                "queued_then_complete_approved": queued_complete["report"],
                "partial": partial["report"],
                "shafafiya": shafafiya["report"],
                "eclaimlink": eclaimlink["report"],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print("Prior Authorization Agent tests passed.")
    print("Output:", OUTPUT_PATH)
    print("Approved:", {"status": approved["status"], "routing": approved["routing"]})
    print("Existing:", {"status": existing["status"], "routing": existing["routing"]})
    print("Not required:", {"status": not_required["status"], "routing": not_required["routing"]})
    print("Missing docs:", {"status": missing_docs["status"], "routing": missing_docs["routing"]})
    print("Pended:", {"status": pended["status"], "routing": pended["routing"]})
    print("Queued complete:", {"status": queued_complete["status"], "routing": queued_complete["routing"]})
    print("Partial:", {"status": partial["status"], "routing": partial["routing"]})
    print("Shafafiya:", {"status": shafafiya["status"], "routing": shafafiya["routing"]})
    print("eClaimLink:", {"status": eclaimlink["status"], "routing": eclaimlink["routing"]})


if __name__ == "__main__":
    main()
