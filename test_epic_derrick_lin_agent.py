import importlib.util
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
AGENT_PATH = ROOT / "Claim Preparation Agent.py"
OUTPUT_PATH = ROOT / "sample_outputs" / "epic_derrick_lin_claim_preparation.json"


def load_agent_module() -> Any:
    spec = importlib.util.spec_from_file_location("claim_preparation_agent", AGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {AGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fhir_get(base_url: str, path: str, token: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/fhir+json, application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def search_first_resource(
    *,
    base_url: str,
    token: str,
    resource_type: str,
    params: dict[str, str],
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    bundle = fhir_get(base_url, f"{resource_type}?{query}", token)
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == resource_type:
            return resource
    raise RuntimeError(f"No {resource_type} resource found for params: {params}")


def search_resources(
    *,
    base_url: str,
    token: str,
    resource_type: str,
    params: dict[str, str],
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(params)
    bundle = fhir_get(base_url, f"{resource_type}?{query}", token)
    return [
        entry["resource"]
        for entry in bundle.get("entry", [])
        if isinstance(entry.get("resource"), dict)
        and entry["resource"].get("resourceType") == resource_type
    ]


def first_text(items: list[dict[str, Any]] | None) -> str | None:
    if not items:
        return None
    item = items[0]
    if item.get("text"):
        return item["text"]
    given = item.get("given", [])
    family = item.get("family")
    parts = [*given, family] if isinstance(given, list) else [given, family]
    return " ".join(str(part) for part in parts if part) or None


def codeable_text(codeable: dict[str, Any] | list[dict[str, Any]] | None) -> str | None:
    if isinstance(codeable, list):
        codeable = codeable[0] if codeable else None
    if not codeable:
        return None
    if codeable.get("text"):
        return codeable["text"]
    coding = codeable.get("coding", [])
    if coding:
        return coding[0].get("display") or coding[0].get("code")
    return None


def main() -> None:
    agent = load_agent_module()
    token = agent.fhir_backend_access_token({})
    if not token:
        raise RuntimeError("Could not get Epic backend token.")

    base_url = (os.getenv("FHIR_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("FHIR_BASE_URL is empty.")

    patient = search_first_resource(
        base_url=base_url,
        token=token,
        resource_type="Patient",
        params={"family": "Lin", "given": "Derrick", "birthdate": "1973-06-03"},
    )
    patient_id = patient["id"]

    encounter = search_first_resource(
        base_url=base_url,
        token=token,
        resource_type="Encounter",
        params={"patient": patient_id, "_count": "5"},
    )

    conditions = search_resources(
        base_url=base_url,
        token=token,
        resource_type="Condition",
        params={"patient": patient_id, "_count": "10"},
    )
    procedures = search_resources(
        base_url=base_url,
        token=token,
        resource_type="Procedure",
        params={"patient": patient_id, "_count": "10"},
    )
    documents = search_resources(
        base_url=base_url,
        token=token,
        resource_type="DocumentReference",
        params={"patient": patient_id, "_count": "10"},
    )

    encounter_type = codeable_text(encounter.get("type")) or "Office Visit"
    initial_state = {
        "case_id": "CASE-EPIC-DERRICK-LIN",
        "trigger_source": "VELO_DOCTOR_APPROVED_ENCOUNTER",
        "use_medgemma": False,
        "claim_format": "CANONICAL",
        "jurisdiction": "INTERNAL",
        "encounter_id": encounter["id"],
        "encounter_package": {
            # Epic sandbox returns no Coverage for this patient, so this is a test fallback.
            "coverage": {
                "resourceType": "Coverage",
                "id": f"COV-{patient_id}",
                "status": "active",
                "subscriberId": f"EPIC-SANDBOX-{patient_id}",
                "payor": [{"display": "Epic Sandbox Payer"}],
                "class": [{"value": "SANDBOX", "name": "Epic Sandbox Plan"}],
            },
            # Epic sandbox metadata does not expose ChargeItem, so this mimics the
            # invoice/activity line Velo Doctor or RCM upload would send.
            "charge_items": [
                {
                    "id": "EPIC-DERRICK-LIN-OV-001",
                    "description": encounter_type,
                    "code": "99213",
                    "system": "CPT",
                    "amount": 150.0,
                    "currency": "USD",
                    "quantity": 1,
                }
            ],
        },
        "payer_rules": [],
    }

    result = agent.run_claim_preparation(initial_state)

    summary = {
        "patient": {
            "id": patient_id,
            "name": first_text(patient.get("name")),
        },
        "selected_encounter": {
            "id": encounter.get("id"),
            "status": encounter.get("status"),
            "type": encounter_type,
            "period": encounter.get("period"),
        },
        "fhir_counts_before_agent": {
            "conditions": len(conditions),
            "procedures": len(procedures),
            "document_references": len(documents),
        },
        "agent_result": {
            "status": result.get("status"),
            "claim_id": result.get("claim_id"),
            "claim_format": result.get("claim_format"),
            "jurisdiction": result.get("jurisdiction"),
            "patient_id": result.get("patient", {}).get("id"),
            "encounter_id": result.get("encounter", {}).get("id"),
            "provider_id": result.get("provider", {}).get("id"),
            "facility_id": result.get("facility", {}).get("id"),
            "conditions_fetched": len(result.get("conditions", [])),
            "procedures_fetched": len(result.get("fhir_procedures", [])),
            "attachments_fetched": len(result.get("attachments", [])),
            "charge_items_used": len(result.get("charge_items", [])),
            "icd_codes": result.get("icd_codes", []),
            "procedure_codes": result.get("procedure_codes", []),
            "missing_fields": result.get("missing_fields", []),
            "warnings": result.get("warnings", []),
            "errors": result.get("errors", []),
        },
        "full_result": result,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print("Epic Derrick Lin agent test complete.")
    print("Output:", OUTPUT_PATH)
    print("Patient:", summary["patient"])
    print("Selected encounter:", summary["selected_encounter"])
    print("FHIR counts before agent:", summary["fhir_counts_before_agent"])
    print("Agent result:", json.dumps(summary["agent_result"], indent=2, default=str))


if __name__ == "__main__":
    main()
