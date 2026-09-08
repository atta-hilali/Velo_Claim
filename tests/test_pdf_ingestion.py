from __future__ import annotations

from fastapi.testclient import TestClient

from velo_claim.api.app import create_app
from velo_claim.core.container import build_default_container
from velo_claim.examples.demo_inputs import abu_dhabi_pneumonia_encounter
from velo_claim.ingestion.pdf_encounter import (
    EncounterPdfExtractor,
    PdfExtractionResult,
    extract_encounter_from_text,
    missing_routing_fields,
)


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


def test_deterministic_encounter_text_extraction_is_pipeline_shaped() -> None:
    package = extract_encounter_from_text(ENCOUNTER_TEXT)

    assert missing_routing_fields(package) == []
    assert package["patient"]["id"] == "PAT-AUH-101"
    assert package["coverage"]["payor"][0]["identifier"]["value"] == "A001"
    assert package["encounter"]["period"]["start"] == "2026-06-16"
    assert package["conditions"][0]["code"]["coding"][0]["code"] == "J18.9"
    assert package["procedures"][0]["code"]["coding"][0]["code"] == "99213"
    assert package["charge_items"][0]["gross"] == 450.0


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
        event.get("event_type") == "PDF_ENCOUNTER_INGESTED"
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
