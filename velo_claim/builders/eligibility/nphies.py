from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


NPHIES_PROFILE_BASE = "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition"
NPHIES_MESSAGE_EVENT_SYSTEM = "http://nphies.sa/terminology/CodeSystem/ksa-message-events"
NPHIES_PAYER_LICENSE_SYSTEM = "http://nphies.sa/license/payer-license"
NPHIES_PROVIDER_LICENSE_SYSTEM = "http://nphies.sa/license/provider-license"
AMA_CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"


class NphiesEligibilityBuilder:
    content_type = "fhir_bundle_json"

    def build(self, canonical_claim: dict[str, Any], source_context: dict[str, Any] | None = None) -> str:
        source_context = source_context or {}
        claim_id = canonical_claim["claim_id"]
        patient = canonical_claim.get("patient", {})
        payer = canonical_claim.get("payer", {})
        provider = canonical_claim.get("provider", {})
        encounter = canonical_claim.get("encounter", {})

        request_id = _fhir_id(f"ELIG-{claim_id}")
        patient_id = _fhir_id(patient.get("id") or f"PAT-{claim_id}")
        coverage_id = _fhir_id(payer.get("coverage_id") or f"COV-{claim_id}")
        provider_org_id = _fhir_id(provider.get("facility_id") or provider.get("facility_license") or f"PROV-{claim_id}")
        insurer_org_id = _fhir_id(payer.get("id") or f"INS-{claim_id}")

        bundle = {
            "resourceType": "Bundle",
            "id": str(uuid4()),
            "meta": {"profile": [_profile("bundle")]},
            "type": "message",
            "timestamp": _now(),
            "entry": [
                _entry(
                    "MessageHeader",
                    {
                        "resourceType": "MessageHeader",
                        "id": _fhir_id(f"MH-{claim_id}"),
                        "meta": {"profile": [_profile("messageheader")]},
                        "eventCoding": {
                            "system": NPHIES_MESSAGE_EVENT_SYSTEM,
                            "code": "eligibility-request",
                        },
                        "source": {
                            "endpoint": os.getenv("NPHIES_SOURCE_ENDPOINT", "https://velodoc.ai/fhir")
                        },
                        "destination": [
                            {
                                "name": payer.get("name"),
                                "receiver": {
                                    "identifier": {
                                        "system": NPHIES_PAYER_LICENSE_SYSTEM,
                                        "value": payer.get("id"),
                                    }
                                },
                            }
                        ],
                        "focus": [{"reference": f"CoverageEligibilityRequest/{request_id}"}],
                    },
                ),
                _entry(
                    "CoverageEligibilityRequest",
                    {
                        "resourceType": "CoverageEligibilityRequest",
                        "id": request_id,
                        "meta": {"profile": [_profile("coverageeligibilityrequest")]},
                        "status": "active",
                        "purpose": ["validation", "benefits"],
                        "patient": {"reference": f"Patient/{patient_id}"},
                        "created": encounter.get("service_date") or _now(),
                        "provider": {"reference": f"Organization/{provider_org_id}"},
                        "insurer": {"reference": f"Organization/{insurer_org_id}"},
                        "insurance": [{"coverage": {"reference": f"Coverage/{coverage_id}"}}],
                        "item": _items(canonical_claim.get("line_items", [])),
                    },
                ),
                _entry("Coverage", _coverage(coverage_id, patient_id, insurer_org_id, payer, patient)),
                _entry("Patient", _patient(patient_id, patient, source_context.get("patient", {}))),
                _entry("Organization", _provider_org(provider_org_id, provider)),
                _entry("Organization", _insurer_org(insurer_org_id, payer)),
            ],
        }
        return json.dumps(bundle, indent=2, ensure_ascii=True)


def _entry(resource_type: str, resource: dict[str, Any]) -> dict[str, Any]:
    resource_id = resource.get("id") or str(uuid4())
    return {"fullUrl": f"{resource_type}/{resource_id}", "resource": resource}


def _coverage(
    coverage_id: str,
    patient_id: str,
    insurer_org_id: str,
    payer: dict[str, Any],
    patient: dict[str, Any],
) -> dict[str, Any]:
    return {
        "resourceType": "Coverage",
        "id": coverage_id,
        "meta": {"profile": [_profile("coverage")]},
        "status": payer.get("coverage_status") or "active",
        "subscriberId": patient.get("member_id") or payer.get("member_id") or payer.get("subscriber_id"),
        "beneficiary": {"reference": f"Patient/{patient_id}"},
        "payor": [{"reference": f"Organization/{insurer_org_id}"}],
        "class": [
            {
                "type": {"text": "plan"},
                "value": payer.get("plan_id") or "UNKNOWN",
            }
        ],
        "period": payer.get("coverage_period") or {},
    }


def _patient(patient_id: str, patient: dict[str, Any], source_patient: dict[str, Any]) -> dict[str, Any]:
    identifiers = []
    if patient.get("member_id"):
        identifiers.append({"system": "velo/member-id", "value": patient["member_id"]})
    if patient.get("emirates_id"):
        identifiers.append({"system": "uae/emirates-id", "value": patient["emirates_id"]})
    for identifier in source_patient.get("identifier", []):
        if identifier not in identifiers:
            identifiers.append(identifier)
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "meta": {"profile": [_profile("patient")]},
        "identifier": identifiers,
        "name": _name(patient.get("name")),
        "birthDate": patient.get("birth_date"),
        "gender": patient.get("gender"),
    }


def _provider_org(provider_org_id: str, provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": provider_org_id,
        "meta": {"profile": [_profile("organization")]},
        "identifier": [
            {
                "system": NPHIES_PROVIDER_LICENSE_SYSTEM,
                "value": provider.get("facility_license") or provider.get("facility_id"),
            }
        ],
        "name": provider.get("facility_name") or provider.get("name"),
    }


def _insurer_org(insurer_org_id: str, payer: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": insurer_org_id,
        "meta": {"profile": [_profile("organization")]},
        "identifier": [{"system": NPHIES_PAYER_LICENSE_SYSTEM, "value": payer.get("id")}],
        "name": payer.get("name") or payer.get("id"),
    }


def _items(line_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for item in line_items:
        code = item.get("code")
        if not code:
            continue
        items.append(
            {
                "category": {"text": item.get("description") or "service"},
                "productOrService": {
                    "coding": [
                        {
                            "system": _procedure_system(item.get("system")),
                            "code": code,
                            "display": item.get("description"),
                        }
                    ]
                },
            }
        )
    return items


def _name(name: str | None) -> list[dict[str, Any]]:
    if not name:
        return []
    parts = str(name).split()
    if len(parts) <= 1:
        return [{"text": name, "family": name}]
    return [{"text": name, "given": parts[:-1], "family": parts[-1]}]


def _profile(name: str) -> str:
    return f"{NPHIES_PROFILE_BASE}/{name}|1.0.0"


def _procedure_system(system: str | None) -> str:
    if str(system or "").upper() in {"CPT", "HCPCS"}:
        return AMA_CPT_SYSTEM
    return system or AMA_CPT_SYSTEM


def _fhir_id(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9\-.]", "-", str(value or uuid4()))
    return text[:64] or str(uuid4())


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
