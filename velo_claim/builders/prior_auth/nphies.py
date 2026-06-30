from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from velo_claim.builders.prior_auth.canonical import PACanonicalForm


NPHIES_PROFILE_BASE = "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition"
NPHIES_MESSAGE_EVENT_SYSTEM = "http://nphies.sa/terminology/CodeSystem/ksa-message-events"
NPHIES_PAYER_LICENSE_SYSTEM = "http://nphies.sa/license/payer-license"
NPHIES_PROVIDER_LICENSE_SYSTEM = "http://nphies.sa/license/provider-license"
NPHIES_PRACTITIONER_LICENSE_SYSTEM = "http://nphies.sa/license/practitioner-license"
AMA_CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"
ICD_10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"


class NphiesPABuilder:
    content_type = "fhir_bundle_json"

    def build(self, pa_form: PACanonicalForm) -> str:
        auth_id = _fhir_id(f"AUTH-{pa_form.claim_id}")
        patient_id = _fhir_id(pa_form.patient.get("id") or f"PAT-{pa_form.claim_id}")
        coverage_id = _fhir_id(pa_form.coverage.get("coverage_id") or f"COV-{pa_form.claim_id}")
        provider_org_id = _fhir_id(pa_form.facility.get("id") or pa_form.facility.get("license") or f"PROV-{pa_form.claim_id}")
        insurer_org_id = _fhir_id(pa_form.payer_id or f"INS-{pa_form.claim_id}")
        practitioner_id = _fhir_id(pa_form.provider.get("id") or pa_form.provider.get("license") or f"PRAC-{pa_form.claim_id}")
        encounter_id = _fhir_id(f"ENC-AUTH-{pa_form.claim_id}")

        claim = {
            "resourceType": "Claim",
            "id": auth_id,
            "meta": {"profile": [_profile("authorization-professional")]},
            "status": "active",
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
            "use": "preauthorization",
            "patient": {"reference": f"Patient/{patient_id}"},
            "created": pa_form.service_date or _now(),
            "provider": {"reference": f"Organization/{provider_org_id}"},
            "insurer": {"reference": f"Organization/{insurer_org_id}"},
            "priority": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/processpriority", "code": "normal"}]},
            "careTeam": [
                {
                    "sequence": 1,
                    "provider": {"reference": f"Practitioner/{practitioner_id}"},
                }
            ],
            "supportingInfo": [
                {
                    "sequence": 1,
                    "category": {"text": "encounter"},
                    "valueReference": {"reference": f"Encounter/{encounter_id}"},
                }
            ],
            "diagnosis": [
                {
                    "sequence": index + 1,
                    "diagnosisCodeableConcept": {
                        "coding": [{"system": ICD_10_SYSTEM, "code": code}],
                    },
                    "type": [
                        {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/ex-diagnosistype",
                                    "code": "principal" if index == 0 else "secondary",
                                }
                            ]
                        }
                    ],
                }
                for index, code in enumerate(pa_form.diagnoses)
            ],
            "insurance": [
                {
                    "sequence": 1,
                    "focal": True,
                    "coverage": {"reference": f"Coverage/{coverage_id}"},
                }
            ],
            "item": [
                {
                    "sequence": index + 1,
                    "careTeamSequence": [1],
                    "productOrService": {
                        "coding": [
                            {
                                "system": _procedure_system(item.get("system")),
                                "code": item.get("code"),
                                "display": item.get("description"),
                            }
                        ]
                    },
                    "quantity": {"value": item.get("quantity", 1)},
                    "net": {"value": float(item.get("net") or item.get("amount") or item.get("gross") or 0.0), "currency": pa_form.currency or "SAR"},
                }
                for index, item in enumerate(pa_form.procedures)
                if item.get("code")
            ],
        }

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
                        "id": _fhir_id(f"MH-{pa_form.claim_id}"),
                        "meta": {"profile": [_profile("messageheader")]},
                        "eventCoding": {
                            "system": NPHIES_MESSAGE_EVENT_SYSTEM,
                            "code": "priorauth-request",
                        },
                        "source": {"endpoint": os.getenv("NPHIES_SOURCE_ENDPOINT", "https://velodoc.ai/fhir")},
                        "destination": [
                            {
                                "receiver": {
                                    "identifier": {
                                        "system": NPHIES_PAYER_LICENSE_SYSTEM,
                                        "value": pa_form.payer_id,
                                    }
                                }
                            }
                        ],
                        "focus": [{"reference": f"Claim/{auth_id}"}],
                    },
                ),
                _entry("Claim", claim),
                _entry("Coverage", _coverage(coverage_id, patient_id, insurer_org_id, pa_form)),
                _entry("Patient", _patient(patient_id, pa_form.patient)),
                _entry("Organization", _provider_org(provider_org_id, pa_form.facility)),
                _entry("Organization", _insurer_org(insurer_org_id, pa_form.payer_id)),
                _entry("Practitioner", _practitioner(practitioner_id, pa_form.provider)),
                _entry("Encounter", _encounter(encounter_id, patient_id, provider_org_id, pa_form)),
            ],
        }
        return json.dumps(bundle, indent=2, ensure_ascii=True)


def _entry(resource_type: str, resource: dict[str, Any]) -> dict[str, Any]:
    resource_id = resource.get("id") or str(uuid4())
    return {"fullUrl": f"{resource_type}/{resource_id}", "resource": resource}


def _coverage(coverage_id: str, patient_id: str, insurer_org_id: str, pa_form: PACanonicalForm) -> dict[str, Any]:
    return {
        "resourceType": "Coverage",
        "id": coverage_id,
        "meta": {"profile": [_profile("coverage")]},
        "status": pa_form.coverage.get("coverage_status") or pa_form.coverage.get("status") or "active",
        "subscriberId": pa_form.patient.get("member_id") or pa_form.coverage.get("subscriber_id"),
        "beneficiary": {"reference": f"Patient/{patient_id}"},
        "payor": [{"reference": f"Organization/{insurer_org_id}"}],
        "class": [{"type": {"text": "plan"}, "value": pa_form.plan_id or "UNKNOWN"}],
        "period": pa_form.coverage.get("coverage_period") or {},
    }


def _patient(patient_id: str, patient: dict[str, Any]) -> dict[str, Any]:
    identifiers = []
    if patient.get("member_id"):
        identifiers.append({"system": "velo/member-id", "value": patient["member_id"]})
    if patient.get("emirates_id"):
        identifiers.append({"system": "uae/emirates-id", "value": patient["emirates_id"]})
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "meta": {"profile": [_profile("patient")]},
        "identifier": identifiers,
        "name": _name(patient.get("name")),
        "birthDate": patient.get("birth_date"),
        "gender": patient.get("gender"),
    }


def _provider_org(provider_org_id: str, facility: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": provider_org_id,
        "meta": {"profile": [_profile("organization")]},
        "identifier": [{"system": NPHIES_PROVIDER_LICENSE_SYSTEM, "value": facility.get("license") or facility.get("id")}],
        "name": facility.get("name"),
    }


def _insurer_org(insurer_org_id: str, payer_id: str) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": insurer_org_id,
        "meta": {"profile": [_profile("organization")]},
        "identifier": [{"system": NPHIES_PAYER_LICENSE_SYSTEM, "value": payer_id}],
        "name": payer_id,
    }


def _practitioner(practitioner_id: str, provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Practitioner",
        "id": practitioner_id,
        "meta": {"profile": [_profile("practitioner")]},
        "identifier": [{"system": NPHIES_PRACTITIONER_LICENSE_SYSTEM, "value": provider.get("license") or provider.get("id")}],
        "name": _name(provider.get("name")),
    }


def _encounter(encounter_id: str, patient_id: str, provider_org_id: str, pa_form: PACanonicalForm) -> dict[str, Any]:
    return {
        "resourceType": "Encounter",
        "id": encounter_id,
        "meta": {"profile": [_profile("encounter-auth-amb")]},
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"reference": f"Patient/{patient_id}"},
        "serviceProvider": {"reference": f"Organization/{provider_org_id}"},
        "period": {"start": pa_form.service_date},
    }


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
