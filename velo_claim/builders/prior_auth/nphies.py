from __future__ import annotations

import json
from uuid import uuid4

from velo_claim.builders.prior_auth.canonical import PACanonicalForm


class NphiesPABuilder:
    content_type = "fhir_bundle_json"

    def build(self, pa_form: PACanonicalForm) -> str:
        bundle = {
            "resourceType": "Bundle",
            "id": str(uuid4()),
            "type": "message",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Claim",
                        "id": f"PA-{pa_form.claim_id}",
                        "status": "active",
                        "use": "preauthorization",
                        "patient": {"identifier": {"value": pa_form.patient.get("id")}},
                        "provider": {"identifier": {"value": pa_form.facility.get("license")}},
                        "insurer": {"identifier": {"value": pa_form.payer_id}},
                        "diagnosis": [
                            {
                                "sequence": index + 1,
                                "diagnosisCodeableConcept": {
                                    "coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": code}]
                                },
                            }
                            for index, code in enumerate(pa_form.diagnoses)
                        ],
                        "item": [
                            {
                                "sequence": index + 1,
                                "productOrService": {
                                    "coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": item.get("code")}]
                                },
                                "quantity": {"value": item.get("quantity", 1)},
                            }
                            for index, item in enumerate(pa_form.procedures)
                        ],
                    }
                }
            ],
        }
        return json.dumps(bundle, indent=2, ensure_ascii=True)
