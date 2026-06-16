import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
AGENT_PATH = ROOT / "Claim Validation Agent.py"
OUTPUT_PATH = ROOT / "sample_outputs" / "claim_validation_test_output.json"


def load_agent_module() -> Any:
    spec = importlib.util.spec_from_file_location("claim_validation_agent", AGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {AGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def clean_claim_state() -> dict[str, Any]:
    return {
        "raw_claim": {
            "claim_id": "CLM-CLEAN-001",
            "patient": {"id": "P-CLEAN-001", "name": "Clean Patient"},
            "payer": {
                "id": "default",
                "name": "Default Payer",
                "coverage_id": "COV-CLEAN-001",
                "coverage_status": "ACTIVE",
            },
            "provider": {"id": "D-CLEAN-001", "facility_id": "FAC-CLEAN-001"},
            "encounter_id": "ENC-CLEAN-001",
            "service_date": "2026-06-10",
            "diagnoses": [
                {"system": "ICD-10", "code": "I10", "description": "Essential hypertension"}
            ],
            "procedures": [
                {"system": "CPT", "code": "99213", "description": "Office visit"}
            ],
            "amount": {"net": 450.0, "currency": "SAR"},
            "clinical_context": {
                "soap_note_excerpt": "Patient presents for hypertension follow-up and medication refill."
            },
            "attachments": [{"type": "soap_note", "name": "soap_note.pdf"}],
            "source_resources": {
                "coverage": {
                    "resourceType": "Coverage",
                    "id": "COV-CLEAN-001",
                    "status": "active",
                    "period": {"start": "2026-01-01", "end": "2026-12-31"},
                },
                "encounter": {
                    "resourceType": "Encounter",
                    "id": "ENC-CLEAN-001",
                    "status": "finished",
                },
            },
        },
        "payer_rules": [
            {
                "rule_id": "DEFAULT-TIMELY-001",
                "payer_id": "default",
                "version": "2026.06.12",
                "status": "ACTIVE",
                "rule_type": "TIMELY_FILING",
                "effective_from": "2026-01-01",
                "condition": {},
                "action": {"limit_days": 90},
                "severity": "FAIL",
                "message": "Claim must be submitted within 90 days.",
            }
        ],
        "submission_date": "2026-06-12",
        "skip_fhir_fetch": True,
    }


def review_claim_state() -> dict[str, Any]:
    state = clean_claim_state()
    claim = state["raw_claim"]
    claim["claim_id"] = "CLM-REVIEW-001"
    claim["payer"]["id"] = "tawuniya"
    claim["payer"]["name"] = "Tawuniya"
    claim["procedures"] = [
        {"system": "CPT", "code": "99214", "description": "Office visit, moderate complexity"}
    ]
    claim["clinical_context"]["soap_note_excerpt"] = (
        "Patient came for mild sore throat. No complex decision making documented."
    )
    claim["attachments"] = [{"type": "soap_note", "name": "soap_note.pdf"}]
    claim["prior_auth"] = None
    state["payer_rules"] = [
        {
            "rule_id": "TAW-PRIOR-99214-001",
            "payer_id": "tawuniya",
            "version": "2026.06.12",
            "status": "ACTIVE",
            "rule_type": "PRIOR_AUTH",
            "effective_from": "2026-01-01",
            "condition": {"cpt": "99214"},
            "action": {},
            "severity": "FAIL",
            "message": "Tawuniya requires prior authorization for CPT 99214.",
        }
    ]
    return state


def hold_claim_state() -> dict[str, Any]:
    state = review_claim_state()
    claim = state["raw_claim"]
    claim["claim_id"] = "CLM-HOLD-001"
    claim["diagnoses"] = [{"system": "ICD-10", "code": "J02.9", "description": "Sore throat"}]
    claim["attachments"] = []
    claim["clinical_context"]["soap_note_excerpt"] = ""
    state["payer_rules"].extend(
        [
            {
                "rule_id": "TAW-CPT-ICD-99214-001",
                "payer_id": "tawuniya",
                "version": "2026.06.12",
                "status": "ACTIVE",
                "rule_type": "CPT_REQUIRES_ICD",
                "effective_from": "2026-01-01",
                "condition": {"cpt": "99214"},
                "action": {"required_icd_prefixes": ["I10", "I11"]},
                "severity": "FAIL",
                "message": "CPT 99214 requires a hypertension-related diagnosis.",
            },
            {
                "rule_id": "TAW-DOC-001",
                "payer_id": "tawuniya",
                "version": "2026.06.12",
                "status": "ACTIVE",
                "rule_type": "DOCUMENTATION",
                "effective_from": "2026-01-01",
                "condition": {},
                "action": {"required_documents": ["soap_note"]},
                "severity": "FAIL",
                "message": "SOAP note is required.",
            },
        ]
    )
    return state


def expired_prior_auth_state() -> dict[str, Any]:
    state = review_claim_state()
    claim = state["raw_claim"]
    claim["claim_id"] = "CLM-EXPIRED-PA-001"
    claim["prior_auth"] = {
        "id": "PA-EXPIRED-001",
        "status": "approved",
        "approved": True,
        "cpt_codes": ["99214"],
        "valid_from": "2026-05-01",
        "valid_to": "2026-06-01",
    }
    return state


def plan_specific_prior_auth_state() -> dict[str, Any]:
    state = clean_claim_state()
    claim = state["raw_claim"]
    claim["claim_id"] = "CLM-PLAN-PA-001"
    claim["payer"] = {
        "id": "DAMAN",
        "name": "DAMAN",
        "plan": "THIQA",
        "coverage_id": "COV-PLAN-001",
        "coverage_status": "ACTIVE",
    }
    claim["source_resources"]["coverage"] = {
        "resourceType": "Coverage",
        "id": "COV-PLAN-001",
        "status": "active",
        "voi_status": "verified",
        "period": {"start": "2026-01-01", "end": "2026-12-31"},
        "payor": [{"display": "DAMAN"}],
        "class": [
            {
                "type": {"text": "plan"},
                "value": "THIQA",
                "name": "Thiqa",
            }
        ],
    }
    state["payer_rules"] = [
        {
            "rule_id": "THIQA-PRIOR-99213-001",
            "payer_id": "*",
            "plan_id": "THIQA",
            "version": "2026.06.16",
            "status": "ACTIVE",
            "rule_type": "PRIOR_AUTH",
            "effective_from": "2026-01-01",
            "condition": {"cpt": "99213"},
            "action": {},
            "severity": "FAIL",
            "message": "Thiqa plan requires prior authorization for CPT 99213.",
        }
    ]
    return state


def kg_mismatch_state() -> dict[str, Any]:
    state = clean_claim_state()
    claim = state["raw_claim"]
    claim["claim_id"] = "CLM-KG-MISMATCH-001"
    claim["diagnoses"] = [
        {"system": "ICD-10", "code": "I10", "description": "Essential hypertension"}
    ]
    claim["procedures"] = [
        {
            "system": "CPT",
            "code": "27447",
            "description": "Total knee arthroplasty",
        }
    ]
    return state


def llm_required_rule_state() -> dict[str, Any]:
    state = clean_claim_state()
    state["use_validation_llm"] = True
    claim = state["raw_claim"]
    claim["claim_id"] = "CLM-LLM-RULE-001"
    state["payer_rules"] = [
        {
            "rule_id": "LLM-MED-NEC-001",
            "payer_id": "default",
            "version": "2026.06.16",
            "status": "ACTIVE",
            "rule_type": "PAYER_RULES",
            "effective_from": "2026-01-01",
            "condition": {},
            "action": {
                "requires_llm": True,
                "llm_prompt_key": "medical_necessity_narrative_check",
                "fix_template": "Add payer-specific medical necessity evidence.",
            },
            "severity": "WARN",
            "message": "Narrative medical necessity requires LLM review.",
        }
    ]
    return state


def main() -> None:
    agent = load_agent_module()

    clean = agent.run_claim_validation(clean_claim_state())
    review = agent.run_claim_validation(review_claim_state())
    hold = agent.run_claim_validation(hold_claim_state())
    expired_pa = agent.run_claim_validation(expired_prior_auth_state())
    plan_pa = agent.run_claim_validation(plan_specific_prior_auth_state())
    kg_mismatch = agent.run_claim_validation(kg_mismatch_state())

    class FakeValidationLLM:
        def evaluate_rule(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "passes": False,
                "summary": "Medical necessity is not clearly supported in the note.",
                "issues": [
                    {
                        "type": "LLM_MEDICAL_NECESSITY_REVIEW",
                        "severity": "WARN",
                        "message": "SOAP note lacks the payer-specific medical necessity statement.",
                        "fix": "Add the missing medical necessity statement.",
                        "evidence": "Fake LLM test evidence",
                        "penalty": 8,
                        "confidence": 0.9,
                    }
                ],
            }

    previous_get_validation_llm = agent.get_validation_llm
    agent.get_validation_llm = lambda state=None: FakeValidationLLM()
    try:
        llm_review = agent.run_claim_validation(llm_required_rule_state())
    finally:
        agent.get_validation_llm = previous_get_validation_llm

    class BrokenTokenManager:
        def access_token(self) -> str:
            raise agent.FHIRAuthError("simulated expired backend token")

        def invalidate(self) -> None:
            return None

    auth_failure_state = clean_claim_state()
    auth_failure_state["skip_fhir_fetch"] = False
    auth_failure_state["raw_claim"]["source_resources"].pop("coverage", None)
    previous_base_url = os.environ.get("FHIR_BASE_URL")
    previous_token_manager = agent.fhir_token_manager
    os.environ["FHIR_BASE_URL"] = "https://example.test/fhir"
    agent.fhir_token_manager = BrokenTokenManager()
    try:
        auth_failure = agent.run_claim_validation(auth_failure_state)
    finally:
        agent.fhir_token_manager = previous_token_manager
        if previous_base_url is None:
            os.environ.pop("FHIR_BASE_URL", None)
        else:
            os.environ["FHIR_BASE_URL"] = previous_base_url

    require(clean["status"] == "CLAIM_VALIDATION_COMPLETED", "Clean claim did not complete")
    require(clean["routing"] == "READY_TO_SUBMIT", f"Unexpected clean routing: {clean['routing']}")
    require(clean["score"] >= 85, f"Unexpected clean score: {clean['score']}")

    require(review["routing"] == "NEEDS_REVIEW", f"Unexpected review routing: {review['routing']}")
    require(
        any(issue["type"] == "MISSING_PRIOR_AUTH" for issue in review["final_issues"]),
        "Review claim should flag missing prior auth",
    )

    require(hold["routing"] == "HOLD_CRITICAL", f"Unexpected hold routing: {hold['routing']}")
    require(hold["score"] < 60, f"Unexpected hold score: {hold['score']}")
    require(
        any(issue["layer"] == "PAYER_RULES" for issue in hold["final_issues"]),
        "Hold claim should flag payer rule issues",
    )
    require(
        any(issue["type"] == "EXPIRED_PRIOR_AUTH" for issue in expired_pa["final_issues"]),
        "Expired prior authorization should be a dedicated validation issue",
    )
    require(
        any(issue["type"] == "MISSING_PRIOR_AUTH" for issue in plan_pa["final_issues"]),
        "Plan-specific prior authorization rule should apply by plan_id",
    )
    require(
        plan_pa["claim"]["plan_id"] == "THIQA",
        "ClaimInput should extract plan_id from payer or coverage",
    )
    require(
        plan_pa["eligibility_result"]["raw_data"]["daman_voi"]["verified"] is True,
        "DAMAN VOI flag should be read from eligibility/coverage context",
    )
    require(
        any(issue["type"] == "KG_ICD_PROCEDURE_MISMATCH" for issue in kg_mismatch["final_issues"]),
        "KG should flag known procedure when claim ICD has no supporting KG edge",
    )
    require(
        any(issue["type"] == "LLM_MEDICAL_NECESSITY_REVIEW" for issue in llm_review["final_issues"]),
        "requires_llm payer rule should call validation LLM and surface its issue",
    )
    require(
        llm_review["payer_rules_result"]["raw_data"]["llm"]["enabled"] is True,
        "LLM raw data should show enabled evaluation",
    )
    require(
        auth_failure["fhir_context_result"]["passes"] is False,
        "FHIR auth failure should fail the FHIR context check",
    )
    require(
        any(issue["type"] == "FHIR_AUTH_FAILED" for issue in auth_failure["final_issues"]),
        "FHIR auth failure should be visible as a validation issue",
    )
    require(
        auth_failure["report"]["checks"]["fhir_context"]["passes"] is False,
        "FHIR auth failure should be visible in the report checks",
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "clean": clean["report"],
                "review": review["report"],
                "hold": hold["report"],
                "expired_prior_auth": expired_pa["report"],
                "plan_specific_prior_auth": plan_pa["report"],
                "kg_mismatch": kg_mismatch["report"],
                "llm_review": llm_review["report"],
                "fhir_auth_failure": auth_failure["report"],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print("Claim Validation Agent tests passed.")
    print("Output:", OUTPUT_PATH)
    print("Clean:", {"score": clean["score"], "routing": clean["routing"]})
    print("Review:", {"score": review["score"], "routing": review["routing"]})
    print("Hold:", {"score": hold["score"], "routing": hold["routing"]})
    print("Expired PA:", {"score": expired_pa["score"], "routing": expired_pa["routing"]})
    print("Plan PA:", {"score": plan_pa["score"], "routing": plan_pa["routing"]})
    print("KG mismatch:", {"score": kg_mismatch["score"], "routing": kg_mismatch["routing"]})
    print("LLM review:", {"score": llm_review["score"], "routing": llm_review["routing"]})
    print(
        "FHIR auth failure:",
        {"score": auth_failure["score"], "routing": auth_failure["routing"]},
    )


if __name__ == "__main__":
    main()
