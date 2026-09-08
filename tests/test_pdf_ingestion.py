from __future__ import annotations

from fastapi.testclient import TestClient

from velo_claim.api.app import create_app
from velo_claim.api.serializers import claim_for_api
from velo_claim.core.container import build_default_container
from velo_claim.examples.demo_inputs import abu_dhabi_pneumonia_encounter
from velo_claim.ingestion.pdf_encounter import (
    EncounterPdfExtractor,
    PdfExtractionResult,
    extract_encounter_from_text,
    missing_routing_fields,
)
from velo_claim.storage.memory import InMemoryRepository


ENCOUNTER_TEXT = """
Patient ID: PAT-AUH-101
Patient Name: Fatima Al Mansoori
Date of Birth: 1988-04-19
Gender: Female
Emirates ID: 784-1988-7654321-2
Member ID: AUH-M-0101
Payer ID: A001
Payer Name: DAMAN
Plan ID: TH4QF
Coverage Start: 2026-01-01
Coverage End: 2026-12-31
Encounter ID: ENC-AUH-101
Service Date: 2026-06-16
Encounter Type: Family medicine consultation
Encounter Class: AMB
Practitioner ID: DR-AUH-101
Practitioner Name: Dr Sara Haddad
Practitioner License: GD6476
Facility ID: FAC-AUH-101
Facility Name: Velo Clinic Abu Dhabi
Facility License: MF2057
Jurisdiction: Abu Dhabi
ICD-10: J18.9 - Pneumonia, unspecified organism
CPT: 99213 - Office outpatient visit Fee: 450.00
"""

DAMAN_TABLE_FORM_TEXT = """
Dubai Hospital
MEDICAL CLAIM FORM
UNITED ARAB EMIRATES
Hospital | Dubai, UAE | DHA Licensed Facility
Regulator: Dubai Health Authority (DHA) / eClaimLink
1. PATIENT INFORMATION
Patient Name
Khalid Al-Mazrouei
Patient ID
AE-PAT-0001
Date of Birth
1990-11-02
Gender
Male
National ID
784-1990-1234567-1
(Emirates ID)
2. COVERAGE / PAYER INFORMATION
Payer
Daman - National Health
Insurance Company
Payer ID
DAMAN-AE-014
Policy Number
POL-DXB-33221
Member ID
MEM-DXB-90011
Plan / Class
Enhanced
Coverage Status
Active
Coverage Period
01 Jan 2026 - 31 Dec 2026
3. PROVIDER & ENCOUNTER DETAILS
Attending Provider
Dr. Fatima Al-Suwaidi
Specialty
Emergency Medicine
License No.
DHA-P-778812
Encounter Type
Emergency / Inpatient Observation
Encounter Status
Finished
Admission
2026-05-02 22:15
Discharge
2026-05-03 04:40
Reason for Visit: Motor vehicle accident - blunt chest trauma
4. DIAGNOSES
S27.9 (ICD-10)
Injury of unspecified intrathoracic organ
Active
5. PROCEDURES / SERVICES
99284 (CPT)
Emergency department visit, high severity
2026-05-02
71260 (CPT)
CT thorax with contrast
2026-05-02
6. CHARGE SUMMARY (Currency: AED)
Description
Code
Qty
Total
Covered
Patient Resp.
Emergency department visit - high severity
99284
1
950.00
855.00
95.00
CT thorax with contrast
71260
1
1,800.00
1,620.00
180.00
TOTAL
2,750.00
2,475.00
275.00
7. SUPPORTING ATTACHMENTS
ct_thorax_20260502.pdf (Radiology Report)
ed_note_20260502.pdf (Physician Note)
Facility: Dubai Hospital, United Arab Emirates
"""


def test_deterministic_encounter_text_extraction_is_pipeline_shaped() -> None:
    package = extract_encounter_from_text(ENCOUNTER_TEXT)

    assert missing_routing_fields(package) == []
    assert package["patient"]["id"] == "PAT-AUH-101"
    assert package["coverage"]["payor"][0]["identifier"]["value"] == "A001"
    assert package["encounter"]["period"]["start"] == "2026-06-16"
    assert package["conditions"][0]["code"]["coding"][0]["code"] == "J18.9"
    assert package["procedures"][0]["code"]["coding"][0]["code"] == "99213"
    assert package["charge_items"][0]["gross"] == 450.0


def test_table_style_daman_claim_form_is_extracted_without_template_specific_coordinates() -> None:
    package = extract_encounter_from_text(DAMAN_TABLE_FORM_TEXT)

    assert missing_routing_fields(package) == []
    assert package["patient"]["id"] == "AE-PAT-0001"
    assert package["patient"]["name"][0]["text"] == "Khalid Al-Mazrouei"
    assert package["coverage"]["payor"][0]["identifier"]["value"] == "DAMAN-AE-014"
    assert package["coverage"]["period"] == {"start": "2026-01-01", "end": "2026-12-31"}
    assert package["encounter"]["period"]["start"] == "2026-05-02T22:15:00"
    assert package["encounter"]["class"]["code"] == "EMER"
    assert package["provider"]["identifier"][0]["value"] == "DHA-P-778812"
    assert package["provider"]["id"] == "DHA-P-778812"
    assert package["facility"]["name"] == "Dubai Hospital"
    assert package["jurisdiction"] == "DUBAI"
    assert [item["code"]["coding"][0]["code"] for item in package["conditions"]] == ["S27.9"]
    assert [item["code"]["coding"][0]["code"] for item in package["procedures"]] == ["99284", "71260"]
    assert package["charge_items"][0]["gross"] == 950.0
    assert package["charge_items"][0]["net"] == 855.0
    assert package["charge_items"][0]["patient_share"] == 95.0
    assert len(package["attachments"]) == 2


def test_table_form_without_ehr_provider_id_completes_pipeline_using_license(monkeypatch) -> None:
    services = build_default_container()
    package = extract_encounter_from_text(DAMAN_TABLE_FORM_TEXT)
    extraction = PdfExtractionResult(package, page_count=2, text_characters=len(DAMAN_TABLE_FORM_TEXT))
    monkeypatch.setattr(EncounterPdfExtractor, "extract", lambda self, content: extraction)

    response = TestClient(create_app(services)).post(
        "/encounters/pdf",
        files={"file": ("daman-form.pdf", b"%PDF-1.7 daman-form", "application/pdf")},
    )

    assert response.status_code == 200, response.text
    claim_id = response.json()["claim_id"]
    assert services.repository.claims[claim_id]["provider_id"] == "DHA-P-778812"


def test_interrupted_pdf_import_without_payload_can_be_retried(monkeypatch) -> None:
    import hashlib

    services = build_default_container()
    content = b"%PDF-1.7 interrupted-daman-form"
    claim_id = f"CLM-PDF-{hashlib.sha256(content).hexdigest()[:12].upper()}"
    services.repository.upsert_claim(
        claim_id,
        {
            "status": "DRAFT_BUILDING",
            "jurisdiction": "DUBAI",
            "payer_id": "DAMAN-AE-014",
            "provider_id": "DHA-P-778812",
            "patient_id": "AE-PAT-0001",
        },
    )
    package = extract_encounter_from_text(DAMAN_TABLE_FORM_TEXT)
    extraction = PdfExtractionResult(package, page_count=2, text_characters=len(DAMAN_TABLE_FORM_TEXT))
    monkeypatch.setattr(EncounterPdfExtractor, "extract", lambda self, content: extraction)

    response = TestClient(create_app(services)).post(
        "/encounters/pdf",
        files={"file": ("daman-form.pdf", content, "application/pdf")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert services.repository.latest_claim_payload(claim_id) is not None


def test_pdf_pipeline_error_does_not_expose_database_details(monkeypatch) -> None:
    import importlib

    app_module = importlib.import_module("velo_claim.api.app")

    services = build_default_container()
    package = extract_encounter_from_text(DAMAN_TABLE_FORM_TEXT)
    extraction = PdfExtractionResult(package, page_count=2, text_characters=len(DAMAN_TABLE_FORM_TEXT))
    monkeypatch.setattr(EncounterPdfExtractor, "extract", lambda self, content: extraction)
    monkeypatch.setattr(
        app_module,
        "run_full_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret SQL constraint detail")),
    )

    response = TestClient(create_app(services)).post(
        "/encounters/pdf",
        files={"file": ("daman-form.pdf", b"%PDF-1.7 failed-pipeline", "application/pdf")},
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "PDF_PIPELINE_FAILED"
    assert detail["error_id"]
    assert "secret SQL" not in response.text


def test_partial_claim_upsert_does_not_erase_known_provider_identity() -> None:
    repository = InMemoryRepository()
    repository.upsert_claim("CLM-1", {"status": "DRAFT_BUILDING", "provider_id": "DHA-P-778812"})
    repository.upsert_claim("CLM-1", {"status": "DRAFT_BUILT", "provider_id": None})

    assert repository.claims["CLM-1"]["provider_id"] == "DHA-P-778812"


def test_incomplete_claim_uses_stored_identifiers_in_queue_display() -> None:
    claim = claim_for_api(
        {
            "claim_id": "CLM-PDF-INCOMPLETE",
            "patient_id": "AE-PAT-0001",
            "payer_id": "DAMAN-AE-014",
            "canonical_claim": {},
        }
    )

    assert claim["patient"] == "AE-PAT-0001"
    assert claim["payer"] == "DAMAN-AE-014"


def test_final_pdf_audit_failure_does_not_fail_completed_import(monkeypatch) -> None:
    services = build_default_container()
    package = extract_encounter_from_text(DAMAN_TABLE_FORM_TEXT)
    extraction = PdfExtractionResult(package, page_count=2, text_characters=len(DAMAN_TABLE_FORM_TEXT))
    monkeypatch.setattr(EncounterPdfExtractor, "extract", lambda self, content: extraction)
    original_insert = services.repository.insert_audit_event

    def fail_only_final_event(claim_id, event):
        if event.get("payload", {}).get("event_name") == "PDF_ENCOUNTER_INGESTED":
            raise RuntimeError("audit sink unavailable")
        return original_insert(claim_id, event)

    monkeypatch.setattr(services.repository, "insert_audit_event", fail_only_final_event)
    response = TestClient(create_app(services)).post(
        "/encounters/pdf",
        files={"file": ("daman-form.pdf", b"%PDF-1.7 audit-failure", "application/pdf")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


def test_pdf_upload_stores_source_and_runs_existing_pipeline(monkeypatch) -> None:
    services = build_default_container()
    package = abu_dhabi_pneumonia_encounter()["source_context"]
    extraction = PdfExtractionResult(package, page_count=1, text_characters=500)
    monkeypatch.setattr(EncounterPdfExtractor, "extract", lambda self, content: extraction)
    client = TestClient(create_app(services))

    response = client.post(
        "/encounters/pdf",
        files={"file": ("encounter.pdf", b"%PDF-1.7 test-content", "application/pdf")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["claim_id"].startswith("CLM-PDF-")
    assert body["claim"]["id"] == body["claim_id"]
    assert any(
        event.get("event_type") == "NODE_EXIT"
        and event.get("payload", {}).get("event_name") == "PDF_ENCOUNTER_INGESTED"
        for event in services.repository.audit_events
    )
    source_uri = body["extraction"]["source_document_uri"]
    assert services.object_store.get_bytes(source_uri).startswith(b"%PDF-")


def test_incomplete_extraction_does_not_start_claim_pipeline(monkeypatch) -> None:
    services = build_default_container()
    extraction = PdfExtractionResult(
        {"patient": {}, "coverage": {}, "encounter": {}, "facility": {}},
        missing_routing_fields=["patient identity", "payer identity", "service date", "facility identity"],
        page_count=1,
    )
    monkeypatch.setattr(EncounterPdfExtractor, "extract", lambda self, content: extraction)
    client = TestClient(create_app(services))

    response = client.post(
        "/encounters/pdf",
        files={"file": ("encounter.pdf", b"%PDF-1.7 test-content", "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PDF_EXTRACTION_INCOMPLETE"
    assert services.repository.claims == {}
