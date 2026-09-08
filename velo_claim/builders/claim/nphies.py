from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4


PROFILE_BASE = "http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition"
MESSAGE_EVENT_SYSTEM = "http://nphies.sa/terminology/CodeSystem/ksa-message-events"
PROVIDER_LICENSE_SYSTEM = "http://nphies.sa/license/provider-license"
PAYER_LICENSE_SYSTEM = "http://nphies.sa/license/payer-license"
PRACTITIONER_LICENSE_SYSTEM = "http://nphies.sa/license/practitioner-license"
PATIENT_IDENTIFIER_SYSTEM = "http://nphies.sa/identifier/patient"
DIAGNOSIS_TYPE_SYSTEM = "http://nphies.sa/terminology/CodeSystem/diagnosis-type"
ORGANIZATION_TYPE_SYSTEM = "http://nphies.sa/terminology/CodeSystem/organization-type"
SUBSCRIBER_RELATIONSHIP_SYSTEM = "http://terminology.hl7.org/CodeSystem/subscriber-relationship"
CLAIM_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/claim-type"
PROCESS_PRIORITY_SYSTEM = "http://terminology.hl7.org/CodeSystem/processpriority"
ENCOUNTER_CLASS_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
ICD_10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
AMA_CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"


class NphiesClaimBuilder:
    content_type = "fhir_bundle_json"

    def build(self, canonical_claim: dict[str, Any]) -> str:
        claim_id = _fhir_id(canonical_claim["claim_id"])
        patient = canonical_claim.get("patient", {})
        payer = canonical_claim.get("payer", {})
        provider = canonical_claim.get("provider", {})
        encounter = canonical_claim.get("encounter", {})

        patient_id = _fhir_id(patient.get("id") or f"PAT-{claim_id}")
        coverage_id = _fhir_id(payer.get("coverage_id") or f"COV-{claim_id}")
        provider_org_id = _fhir_id(
            provider.get("facility_id") or provider.get("facility_license") or f"PROV-{claim_id}"
        )
        insurer_org_id = _fhir_id(payer.get("id") or f"INS-{claim_id}")
        practitioner_id = _fhir_id(
            provider.get("id") or provider.get("license") or f"PRAC-{claim_id}"
        )
        encounter_id = _fhir_id(encounter.get("id") or f"ENC-{claim_id}")
        encounter_code = _encounter_code(encounter)
        claim_type = _claim_type(canonical_claim)

        resources = [
            _message_header(claim_id, provider, payer),
            _provider_organization(provider_org_id, provider),
            _insurer_organization(insurer_org_id, payer),
            _practitioner(practitioner_id, provider),
            _patient(patient_id, patient),
            _coverage(coverage_id, patient_id, insurer_org_id, patient, payer),
            _encounter(
                encounter_id,
                patient_id,
                practitioner_id,
                provider_org_id,
                encounter,
                encounter_code,
            ),
            _claim(
                claim_id,
                patient_id,
                coverage_id,
                provider_org_id,
                insurer_org_id,
                practitioner_id,
                encounter_id,
                canonical_claim,
                claim_type,
            ),
        ]
        bundle = {
            "resourceType": "Bundle",
            "id": str(uuid4()),
            "meta": {"profile": [_profile("bundle")]},
            "type": "message",
            "timestamp": _now(),
            "entry": [_entry(resource) for resource in resources],
        }
        return json.dumps(bundle, indent=2, ensure_ascii=True)


def _message_header(claim_id: str, provider: dict[str, Any], payer: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "MessageHeader",
        "id": str(uuid4()),
        "meta": {"profile": [_profile("message-header")]},
        "eventCoding": {"system": MESSAGE_EVENT_SYSTEM, "code": "claim-request"},
        "source": {"endpoint": _env_url("NPHIES_SOURCE_ENDPOINT", "https://velodoc.ai/fhir")},
        "destination": [
            {
                "endpoint": _env_url(
                    "NPHIES_PROCESS_MESSAGE_URL",
                    "https://nphies.sa/fhir/$process-message",
                ),
                "receiver": {
                    "type": "Organization",
                    "identifier": {
                        "system": PAYER_LICENSE_SYSTEM,
                        "use": "official",
                        "value": payer.get("id"),
                    },
                },
            }
        ],
        "sender": {
            "type": "Organization",
            "identifier": {
                "system": PROVIDER_LICENSE_SYSTEM,
                "use": "official",
                "value": provider.get("facility_license") or provider.get("facility_id"),
            },
        },
        "focus": [{"reference": f"Claim/{claim_id}"}],
    }


def _provider_organization(resource_id: str, provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": resource_id,
        "meta": {"profile": [_profile("provider-organization")]},
        "identifier": [
            {
                "system": PROVIDER_LICENSE_SYSTEM,
                "use": "official",
                "value": provider.get("facility_license") or provider.get("facility_id"),
            }
        ],
        "type": [
            {"coding": [{"system": ORGANIZATION_TYPE_SYSTEM, "code": "prov", "display": "Provider"}]}
        ],
        "name": provider.get("facility_name") or provider.get("name"),
    }


def _insurer_organization(resource_id: str, payer: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": resource_id,
        "meta": {"profile": [_profile("insurer-organization")]},
        "identifier": [
            {"system": PAYER_LICENSE_SYSTEM, "use": "official", "value": payer.get("id")}
        ],
        "type": [
            {"coding": [{"system": ORGANIZATION_TYPE_SYSTEM, "code": "ins", "display": "Insurer"}]}
        ],
        "name": payer.get("name") or payer.get("id"),
    }


def _practitioner(resource_id: str, provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Practitioner",
        "id": resource_id,
        "meta": {"profile": [_profile("practitioner")]},
        "identifier": [
            {
                "system": PRACTITIONER_LICENSE_SYSTEM,
                "use": "official",
                "value": provider.get("license") or provider.get("id"),
            }
        ],
        "name": _name(provider.get("name")),
    }


def _patient(resource_id: str, patient: dict[str, Any]) -> dict[str, Any]:
    identifier = (
        patient.get("national_identifier")
        or patient.get("national_id")
        or patient.get("iqama")
        or patient.get("emirates_id")
        or patient.get("member_id")
    )
    return _drop_empty(
        {
            "resourceType": "Patient",
            "id": resource_id,
            "meta": {"profile": [_profile("patient")]},
            "identifier": [
                {"system": PATIENT_IDENTIFIER_SYSTEM, "use": "official", "value": identifier}
            ],
            "name": _name(patient.get("name")),
            "birthDate": patient.get("birth_date"),
            "gender": patient.get("gender"),
        }
    )


def _coverage(
    resource_id: str,
    patient_id: str,
    insurer_org_id: str,
    patient: dict[str, Any],
    payer: dict[str, Any],
) -> dict[str, Any]:
    return _drop_empty(
        {
            "resourceType": "Coverage",
            "id": resource_id,
            "meta": {"profile": [_profile("coverage")]},
            "status": payer.get("coverage_status") or "active",
            "subscriberId": patient.get("member_id"),
            "subscriber": {"reference": f"Patient/{patient_id}"},
            "beneficiary": {"reference": f"Patient/{patient_id}"},
            "relationship": {
                "coding": [{"system": SUBSCRIBER_RELATIONSHIP_SYSTEM, "code": "self"}]
            },
            "payor": [{"reference": f"Organization/{insurer_org_id}"}],
            "class": [{"type": {"text": "plan"}, "value": payer.get("plan_id") or "UNKNOWN"}],
            "period": payer.get("coverage_period") or {},
        }
    )


def _encounter(
    resource_id: str,
    patient_id: str,
    practitioner_id: str,
    provider_org_id: str,
    encounter: dict[str, Any],
    encounter_code: str,
) -> dict[str, Any]:
    return {
        "resourceType": "Encounter",
        "id": resource_id,
        "meta": {"profile": [_profile(f"encounter-claim-{encounter_code.lower()}")]},
        "status": encounter.get("status") or "finished",
        "class": {"system": ENCOUNTER_CLASS_SYSTEM, "code": encounter_code},
        "subject": {"reference": f"Patient/{patient_id}"},
        "participant": [{"individual": {"reference": f"Practitioner/{practitioner_id}"}}],
        "serviceProvider": {"reference": f"Organization/{provider_org_id}"},
        "period": _encounter_period(encounter),
    }


def _claim(
    claim_id: str,
    patient_id: str,
    coverage_id: str,
    provider_org_id: str,
    insurer_org_id: str,
    practitioner_id: str,
    encounter_id: str,
    canonical_claim: dict[str, Any],
    claim_type: str,
) -> dict[str, Any]:
    insurance: dict[str, Any] = {
        "sequence": 1,
        "focal": True,
        "coverage": {"reference": f"Coverage/{coverage_id}"},
    }
    if canonical_claim.get("pre_auth_ref"):
        insurance["preAuthRef"] = [canonical_claim["pre_auth_ref"]]

    return {
        "resourceType": "Claim",
        "id": claim_id,
        "meta": {"profile": [_profile(f"{claim_type}-claim")]},
        "extension": [
            {
                "url": _profile("extension-encounter"),
                "valueReference": {"reference": f"Encounter/{encounter_id}"},
            }
        ],
        "status": "active",
        "type": {"coding": [{"system": CLAIM_TYPE_SYSTEM, "code": claim_type}]},
        "use": "claim",
        "patient": {"reference": f"Patient/{patient_id}"},
        "created": _now(),
        "provider": {"reference": f"Organization/{provider_org_id}"},
        "insurer": {"reference": f"Organization/{insurer_org_id}"},
        "priority": {"coding": [{"system": PROCESS_PRIORITY_SYSTEM, "code": "normal"}]},
        "careTeam": [
            {"sequence": 1, "provider": {"reference": f"Practitioner/{practitioner_id}"}}
        ],
        "insurance": [insurance],
        "diagnosis": [
            {
                "sequence": index + 1,
                "diagnosisCodeableConcept": {
                    "coding": [
                        {
                            "system": ICD_10_SYSTEM,
                            "code": item.get("code"),
                            "display": item.get("description"),
                        }
                    ]
                },
                "type": [
                    {
                        "coding": [
                            {
                                "system": DIAGNOSIS_TYPE_SYSTEM,
                                "code": "principal" if index == 0 else "secondary",
                            }
                        ]
                    }
                ],
            }
            for index, item in enumerate(canonical_claim.get("diagnoses", []))
            if item.get("code")
        ],
        "item": [
            {
                "sequence": index + 1,
                "careTeamSequence": [1],
                "productOrService": {
                    "coding": [
                        {
                            "system": _procedure_system(line.get("system")),
                            "code": line.get("code"),
                            "display": line.get("description"),
                        }
                    ]
                },
                "servicedDate": _date_only(line.get("service_date")),
                "quantity": {"value": line.get("quantity", 1)},
                "net": {
                    "value": _money(line.get("net")),
                    "currency": line.get("currency") or "SAR",
                },
            }
            for index, line in enumerate(canonical_claim.get("line_items", []))
            if line.get("code")
        ],
        "total": {
            "value": _money(canonical_claim.get("amount", {}).get("net")),
            "currency": canonical_claim.get("amount", {}).get("currency") or "SAR",
        },
    }


def _entry(resource: dict[str, Any]) -> dict[str, Any]:
    return {"fullUrl": f"{resource['resourceType']}/{resource['id']}", "resource": resource}


def _profile(name: str) -> str:
    return f"{PROFILE_BASE}/{name}|1.0.0"


def _claim_type(claim: dict[str, Any]) -> str:
    explicit = str(claim.get("claim_type") or "").strip().lower()
    if explicit in {"professional", "institutional", "oral", "pharmacy", "vision"}:
        return explicit
    return "professional"


def _encounter_code(encounter: dict[str, Any]) -> str:
    raw = str(encounter.get("class_code") or encounter.get("type") or "AMB").upper()
    for code in ("AMB", "EMER", "HH", "IMP", "SS", "VR"):
        if code in raw:
            return code
    return "AMB"


def _encounter_period(encounter: dict[str, Any]) -> dict[str, str]:
    source = encounter.get("period") or {}
    start = source.get("start") or encounter.get("service_date") or _now()
    end = source.get("end")
    if not end:
        parsed = _parse_datetime(start)
        end = (parsed + timedelta(minutes=30)).isoformat() if parsed else start
    return {"start": str(start), "end": str(end)}


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _name(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        given = value.get("given") or []
        family = value.get("family")
        text = value.get("text") or " ".join([*given, family] if family else given)
        return [_drop_empty({"text": text, "given": given, "family": family})]
    text = str(value or "").strip()
    if not text:
        return []
    parts = text.split()
    return [{"text": text, "given": parts[:-1] or [parts[0]], "family": parts[-1]}]


def _procedure_system(value: object) -> str:
    system = str(value or "").strip()
    return AMA_CPT_SYSTEM if not system or system.upper() in {"CPT", "HCPCS"} else system


def _date_only(value: object) -> str | None:
    return str(value).split("T")[0] if value else None


def _money(value: object) -> float:
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fhir_id(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9\-.]", "-", str(value or uuid4()))
    return text[:64] or str(uuid4())


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _env_url(name: str, default: str) -> str:
    return os.getenv(name) or default


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}
