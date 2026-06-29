from __future__ import annotations

import json
from uuid import uuid4


class NphiesClaimBuilder:
    content_type = "fhir_bundle_json"

    def build(self, canonical_claim: dict) -> str:
        claim_id = canonical_claim["claim_id"]
        patient_id = canonical_claim["patient"].get("id") or "UNKNOWN"
        provider_id = canonical_claim["provider"].get("facility_license") or canonical_claim["provider"].get("facility_id") or "UNKNOWN"
        bundle = {
            "resourceType": "Bundle",
            "id": str(uuid4()),
            "meta": {"profile": ["http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/bundle|1.0.0"]},
            "type": "message",
            "entry": [
                {
                    "fullUrl": f"urn:uuid:{uuid4()}",
                    "resource": {
                        "resourceType": "MessageHeader",
                        "eventCoding": {
                            "system": "http://nphies.sa/terminology/CodeSystem/ksa-message-events",
                            "code": "claim-request",
                        },
                        "source": {"endpoint": "https://veloclaim.local/fhir"},
                        "destination": [
                            {
                                "name": canonical_claim["payer"].get("name"),
                                "receiver": {"identifier": {"system": "http://nphies.sa/license/payer-license", "value": canonical_claim["payer"].get("id")}},
                            }
                        ],
                        "focus": [{"reference": f"Claim/{claim_id}"}],
                    },
                },
                {
                    "fullUrl": f"Claim/{claim_id}",
                    "resource": {
                        "resourceType": "Claim",
                        "id": claim_id,
                        "meta": {"profile": ["http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/claim|1.0.0"]},
                        "status": "active",
                        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
                        "use": "claim",
                        "patient": {"reference": f"Patient/{patient_id}"},
                        "created": canonical_claim["encounter"].get("service_date"),
                        "provider": {"identifier": {"value": provider_id}},
                        "insurer": {"identifier": {"value": canonical_claim["payer"].get("id")}},
                        "priority": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/processpriority", "code": "normal"}]},
                        "diagnosis": [
                            {
                                "sequence": index + 1,
                                "diagnosisCodeableConcept": {
                                    "coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": item["code"], "display": item.get("description")}]
                                },
                                "type": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/ex-diagnosistype", "code": item.get("type", "principal")}]}],
                            }
                            for index, item in enumerate(canonical_claim.get("diagnoses", []))
                        ],
                        "item": [
                            {
                                "sequence": index + 1,
                                "productOrService": {
                                    "coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": line["code"], "display": line.get("description")}]
                                },
                                "quantity": {"value": line.get("quantity", 1)},
                                "net": {"value": line.get("net", 0.0), "currency": line.get("currency", "SAR")},
                            }
                            for index, line in enumerate(canonical_claim.get("line_items", []))
                        ],
                        "total": {"value": canonical_claim["amount"].get("net", 0.0), "currency": canonical_claim["amount"].get("currency", "SAR")},
                    },
                },
            ],
        }
        return json.dumps(bundle, indent=2, ensure_ascii=True)
