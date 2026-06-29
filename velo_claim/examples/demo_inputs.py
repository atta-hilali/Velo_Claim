def abu_dhabi_pneumonia_encounter() -> dict:
    return {
        "claim_id": "CLM-CLEAN-AUH-001",
        "source_context": {
            "patient": {
                "resourceType": "Patient",
                "id": "PAT-AUH-001",
                "identifier": [
                    {"system": "velo/member-id", "value": "AUH-M-0001"},
                    {"system": "uae/emirates-id", "value": "784-1988-7654321-2"},
                ],
                "name": [{"text": "Fatima Al Mansoori"}],
                "birthDate": "1988-04-19",
                "gender": "female",
            },
            "coverage": {
                "resourceType": "Coverage",
                "id": "COV-AUH-001",
                "status": "active",
                "subscriberId": "AUH-M-0001",
                "voi_verified": True,
                "payor": [{"identifier": {"value": "A001"}, "display": "DAMAN"}],
                "class": [{"type": {"text": "plan"}, "value": "TH4QF", "name": "Thiqa"}],
                "period": {"start": "2026-01-01", "end": "2026-12-31"},
            },
            "encounter": {
                "resourceType": "Encounter",
                "id": "ENC-AUH-001",
                "status": "finished",
                "type": [{"text": "Family medicine consultation"}],
                "period": {"start": "2026-06-16T09:15:00+04:00", "end": "2026-06-16T09:45:00+04:00"},
                "subject": {"reference": "Patient/PAT-AUH-001"},
                "participant": [{"individual": {"reference": "Practitioner/DR-AUH-001"}}],
                "serviceProvider": {"reference": "Organization/FAC-AUH-001"},
            },
            "provider": {
                "resourceType": "Practitioner",
                "id": "DR-AUH-001",
                "identifier": [{"system": "doh/clinician-license", "value": "GD6476"}],
                "name": [{"text": "Dr. Sara Haddad"}],
            },
            "facility": {
                "resourceType": "Organization",
                "id": "FAC-AUH-001",
                "name": "Velo Clinic Abu Dhabi",
                "identifier": [{"system": "doh/facility-license", "value": "MF2057"}],
            },
            "conditions": [
                {
                    "resourceType": "Condition",
                    "id": "COND-AUH-001",
                    "code": {
                        "coding": [
                            {
                                "system": "http://hl7.org/fhir/sid/icd-10",
                                "code": "J18.9",
                                "display": "Pneumonia, unspecified organism",
                            }
                        ]
                    },
                }
            ],
            "procedures": [
                {
                    "resourceType": "Procedure",
                    "id": "PROC-AUH-001",
                    "code": {
                        "coding": [
                            {
                                "system": "http://www.ama-assn.org/go/cpt",
                                "code": "99213",
                                "display": "Office outpatient visit",
                            }
                        ]
                    },
                    "performedDateTime": "2026-06-16T09:20:00+04:00",
                }
            ],
            "attachments": [{"type": "SOAP_NOTE", "name": "soap_note.pdf"}],
            "charge_items": [
                {
                    "id": "ACT-AUH-001",
                    "code": "99213",
                    "system": "CPT",
                    "description": "Office outpatient visit",
                    "quantity": 1,
                    "gross": 450.0,
                    "patient_share": 0.0,
                    "net": 450.0,
                    "currency": "AED",
                }
            ],
        },
    }
