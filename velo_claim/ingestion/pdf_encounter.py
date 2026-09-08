from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


class PdfExtractionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class PdfExtractionResult:
    encounter_package: dict[str, Any]
    missing_routing_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extraction_method: str = "deterministic"
    page_count: int = 0
    text_characters: int = 0

    @property
    def ready(self) -> bool:
        return not self.missing_routing_fields

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ready": self.ready}


class EncounterPdfExtractor:
    def __init__(self, *, max_pages: int | None = None) -> None:
        self.max_pages = max_pages or int(os.getenv("PDF_ENCOUNTER_MAX_PAGES", "100"))

    def extract(self, content: bytes) -> PdfExtractionResult:
        text, page_count = extract_pdf_text(content, max_pages=self.max_pages)
        warnings: list[str] = []
        method = "deterministic"
        package = extract_encounter_from_text(text)

        if _llm_enabled():
            try:
                llm_package = _extract_with_llm(text)
                package = _fill_missing(package, llm_package)
                method = "deterministic+llm"
            except Exception:
                warnings.append("The configured extraction model was unavailable; deterministic extraction was used.")

        missing = missing_routing_fields(package)
        if not package.get("conditions"):
            warnings.append("No diagnosis code was extracted; validation will require RCM review.")
        if not package.get("procedures"):
            warnings.append("No procedure code was extracted; validation will require RCM review.")
        if not package.get("charge_items"):
            warnings.append("No financial charge line was extracted; validation will require RCM review.")
        return PdfExtractionResult(
            encounter_package=package,
            missing_routing_fields=missing,
            warnings=warnings,
            extraction_method=method,
            page_count=page_count,
            text_characters=len(text),
        )


def extract_pdf_text(content: bytes, *, max_pages: int = 100) -> tuple[str, int]:
    if not content.startswith(b"%PDF-"):
        raise PdfExtractionError("INVALID_PDF", "The uploaded file does not have a valid PDF signature.")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install pypdf to extract encounter PDFs.") from exc
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
    except Exception as exc:
        raise PdfExtractionError("INVALID_PDF", "The PDF could not be parsed safely.") from exc
    if reader.is_encrypted:
        raise PdfExtractionError("ENCRYPTED_PDF", "Encrypted encounter PDFs are not supported.")
    page_count = len(reader.pages)
    if page_count == 0 or page_count > max_pages:
        raise PdfExtractionError("PDF_PAGE_LIMIT", f"PDF must contain between 1 and {max_pages} pages.")
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if len(re.sub(r"\s", "", text)) < 30:
        ocr_text = _extract_with_ocr(content)
        if not ocr_text:
            raise PdfExtractionError(
                "PDF_OCR_REQUIRED",
                "The PDF has no usable text layer. Configure PDF_OCR_ENDPOINT or upload a searchable PDF.",
            )
        text = ocr_text
    return text, page_count


def extract_encounter_from_text(text: str) -> dict[str, Any]:
    patient_id = _field(text, "Patient ID", "MRN", "Medical Record Number")
    patient_name = _field(text, "Patient Name", "Full Name")
    member_id = _field(text, "Member ID", "Membership ID", "Subscriber ID")
    national_id = _field(text, "National ID", "National Identifier", "Iqama")
    emirates_id = _field(text, "Emirates ID", "EmiratesIDNumber")
    birth_date = _date(_field(text, "Date of Birth", "DOB", "Birth Date"))
    gender = _gender(_field(text, "Gender", "Sex"))

    payer_id = _field(text, "Payer ID", "Insurer ID", "Receiver ID")
    payer_name = _field(text, "Payer Name", "Insurer Name", "Insurance Company")
    plan_id = _field(text, "Plan ID", "Policy ID", "Plan Code")
    coverage_status = (_field(text, "Coverage Status", "Eligibility Status") or "active").lower()
    coverage_start = _date(_field(text, "Coverage Start", "Policy Start"))
    coverage_end = _date(_field(text, "Coverage End", "Policy End", "Policy Expiry"))

    encounter_id = _field(text, "Encounter ID", "Visit ID", "Episode ID")
    service_date = _date(_field(text, "Service Date", "Date of Service", "Encounter Date", "Visit Date"))
    encounter_start = _datetime(_field(text, "Encounter Start", "Visit Start", "Admission Date")) or service_date
    encounter_end = _datetime(_field(text, "Encounter End", "Visit End", "Discharge Date")) or encounter_start
    encounter_type = _field(text, "Encounter Type", "Visit Type") or "Ambulatory"
    class_code = (_field(text, "Encounter Class", "Class Code") or "AMB").upper()
    jurisdiction = _jurisdiction(_field(text, "Jurisdiction", "Emirate", "Country"))

    provider_id = _field(text, "Practitioner ID", "Clinician ID", "Provider ID")
    provider_name = _field(text, "Practitioner Name", "Clinician Name", "Provider Name", "Doctor Name")
    provider_license = _field(text, "Practitioner License", "Clinician License", "Provider License")
    facility_id = _field(text, "Facility ID", "Organization ID")
    facility_name = _field(text, "Facility Name", "Clinic Name", "Hospital Name")
    facility_license = _field(text, "Facility License", "Facility Code", "Sender ID")

    diagnoses = _diagnoses(text)
    procedures = _procedures(text, service_date)
    charges = _charges(text, procedures, service_date, jurisdiction)
    patient_identifiers = []
    if member_id:
        patient_identifiers.append({"system": "velo/member-id", "value": member_id})
    if emirates_id:
        patient_identifiers.append({"system": "uae/emirates-id", "value": emirates_id})
    if national_id:
        patient_identifiers.append({"system": "ksa/national-id", "value": national_id})

    package: dict[str, Any] = {
        "patient": {
            "resourceType": "Patient",
            "id": patient_id,
            "identifier": patient_identifiers,
            "name": [{"text": patient_name}] if patient_name else [],
            "birthDate": birth_date,
            "gender": gender,
        },
        "coverage": {
            "resourceType": "Coverage",
            "status": coverage_status,
            "subscriberId": member_id,
            "payor": [{"identifier": {"value": payer_id}, "display": payer_name}],
            "class": [{"type": {"text": "plan"}, "value": plan_id}],
            "period": {key: value for key, value in {"start": coverage_start, "end": coverage_end}.items() if value},
        },
        "encounter": {
            "resourceType": "Encounter",
            "id": encounter_id,
            "status": "finished",
            "type": [{"text": encounter_type}],
            "class": {"code": class_code},
            "period": {key: value for key, value in {"start": encounter_start, "end": encounter_end}.items() if value},
        },
        "provider": {
            "resourceType": "Practitioner",
            "id": provider_id,
            "name": [{"text": provider_name}] if provider_name else [],
            "identifier": ([{"system": _provider_system(jurisdiction), "value": provider_license}] if provider_license else []),
        },
        "facility": {
            "resourceType": "Organization",
            "id": facility_id,
            "name": facility_name,
            "identifier": ([{"system": _facility_system(jurisdiction), "value": facility_license}] if facility_license else []),
        },
        "conditions": diagnoses,
        "procedures": procedures,
        "charge_items": charges,
        "attachments": [],
    }
    return _remove_none(package)


def missing_routing_fields(package: dict[str, Any]) -> list[str]:
    patient = package.get("patient", {})
    coverage = package.get("coverage", {})
    encounter = package.get("encounter", {})
    facility = package.get("facility", {})
    payer = (coverage.get("payor") or [{}])[0]
    missing = []
    if not patient.get("id") and not patient.get("name") and not patient.get("identifier"):
        missing.append("patient identity")
    if not payer.get("identifier", {}).get("value") and not payer.get("display"):
        missing.append("payer identity")
    if not encounter.get("period", {}).get("start"):
        missing.append("service date")
    if not facility.get("id") and not facility.get("identifier") and not facility.get("name"):
        missing.append("facility identity")
    return missing


def _field(text: str, *labels: str) -> str | None:
    alternatives = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?im)^\s*(?:{alternatives})\s*[:#\-]\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else None


def _diagnoses(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(?im)^\s*(?:Diagnosis(?:\s+Code)?|ICD(?:-?10(?:-AM)?)?)\s*[:#\-]?\s*([A-Z]\d{2}(?:\.\w{1,4})?)\s*(?:[-:]\s*(.*))?$"
    )
    seen: set[str] = set()
    result = []
    for match in pattern.finditer(text):
        code = match.group(1).upper()
        if code in seen:
            continue
        seen.add(code)
        result.append(
            {
                "resourceType": "Condition",
                "code": {
                    "coding": [
                        {
                            "system": "http://hl7.org/fhir/sid/icd-10",
                            "code": code,
                            "display": (match.group(2) or "").strip() or None,
                        }
                    ]
                },
            }
        )
    return _remove_none(result)


def _procedures(text: str, service_date: str | None) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(?im)^\s*(CPT|CDT|HCPCS|Procedure(?:\s+Code)?)\s*[:#\-]?\s*([A-Z]?\d{4,5})\s*(?:[-:]\s*(.*))?$"
    )
    seen: set[str] = set()
    result = []
    for match in pattern.finditer(text):
        code = match.group(2).upper()
        if code in seen:
            continue
        seen.add(code)
        named_system = match.group(1).upper()
        system = "CDT" if named_system == "CDT" or code.startswith("D") else "CPT"
        result.append(
            {
                "resourceType": "Procedure",
                "code": {"coding": [{"system": system, "code": code, "display": (match.group(3) or "").strip() or None}]},
                "performedDateTime": service_date,
            }
        )
    return _remove_none(result)


def _charges(
    text: str,
    procedures: list[dict[str, Any]],
    service_date: str | None,
    jurisdiction: str | None,
) -> list[dict[str, Any]]:
    currency = "SAR" if jurisdiction == "KSA" else "AED"
    result = []
    for procedure in procedures:
        coding = procedure.get("code", {}).get("coding", [{}])[0]
        code = coding.get("code")
        same_line = re.search(
            rf"(?im)^.*\b{re.escape(str(code))}\b.*?(?:AED|SAR|Amount|Fee|Gross)\s*[: ]\s*([0-9]+(?:\.[0-9]{{1,2}})?).*$",
            text,
        )
        amount = float(same_line.group(1)) if same_line else None
        result.append(
            {
                "code": code,
                "system": coding.get("system"),
                "description": coding.get("display"),
                "quantity": 1,
                "gross": amount,
                "patient_share": 0.0 if amount is not None else None,
                "net": amount,
                "currency": currency,
                "service_date": service_date,
                "missing_financial_fields": [] if amount is not None else ["gross", "net"],
            }
        )
    return _remove_none(result)


def _date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.split()[0], fmt).date().isoformat()
        except ValueError:
            continue
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _datetime(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        parsed_date = _date(text)
        return parsed_date


def _gender(value: str | None) -> str | None:
    token = str(value or "").strip().lower()
    return {"m": "male", "male": "male", "f": "female", "female": "female"}.get(token)


def _jurisdiction(value: str | None) -> str | None:
    token = re.sub(r"[^a-z]", "", str(value or "").lower())
    if token in {"ksa", "saudi", "saudiarabia"}:
        return "KSA"
    if token in {"dubai", "dha"}:
        return "DUBAI"
    if token in {"abudhabi", "doh"}:
        return "ABU_DHABI"
    return None


def _facility_system(jurisdiction: str | None) -> str:
    return {"KSA": "nphies/provider-id", "DUBAI": "dha/facility-code", "ABU_DHABI": "doh/facility-license"}.get(
        jurisdiction or "", "facility-license"
    )


def _provider_system(jurisdiction: str | None) -> str:
    return {"KSA": "nphies/practitioner-id", "DUBAI": "dha/clinician-license", "ABU_DHABI": "doh/clinician-license"}.get(
        jurisdiction or "", "provider-license"
    )


def _remove_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _remove_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_remove_none(item) for item in value]
    return value


def _fill_missing(primary: Any, fallback: Any) -> Any:
    if isinstance(primary, dict) and isinstance(fallback, dict):
        return {key: _fill_missing(primary.get(key), value) if key in primary else value for key, value in fallback.items()} | {
            key: value for key, value in primary.items() if key not in fallback
        }
    if primary in (None, "", [], {}):
        return fallback
    return primary


def _llm_enabled() -> bool:
    return os.getenv("PDF_ENCOUNTER_USE_LLM", "false").lower() in {"1", "true", "yes"} and bool(
        os.getenv("ENCOUNTER_EXTRACTION_LLM_BASE_URL") or os.getenv("MEDGEMMA_BASE_URL")
    )


def _extract_with_llm(text: str) -> dict[str, Any]:
    base_url = os.getenv("ENCOUNTER_EXTRACTION_LLM_BASE_URL") or os.getenv("MEDGEMMA_BASE_URL", "")
    model = os.getenv("ENCOUNTER_EXTRACTION_LLM_MODEL") or os.getenv("MEDGEMMA_MODEL", "medgemma")
    prompt = (
        "Extract a healthcare encounter into strict JSON with one key encounter_package containing patient, coverage, "
        "encounter, provider, facility, conditions, procedures, charge_items and attachments. Use null for absent facts. "
        "Never infer identifiers, codes, dates, amounts or coverage status. Return JSON only.\n\nDOCUMENT:\n" + text[:60000]
    )
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(
                {"Authorization": f"Bearer {os.getenv('ENCOUNTER_EXTRACTION_LLM_API_KEY') or os.getenv('MEDGEMMA_API_KEY')}"}
                if os.getenv("ENCOUNTER_EXTRACTION_LLM_API_KEY") or os.getenv("MEDGEMMA_API_KEY")
                else {}
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = result.get("choices", [{}])[0].get("message", {}).get("content", "{}")
    decoded = json.loads(content)
    package = decoded.get("encounter_package") if isinstance(decoded, dict) else None
    if not isinstance(package, dict):
        raise ValueError("Extraction model did not return encounter_package.")
    return _remove_none(package)


def _extract_with_ocr(content: bytes) -> str | None:
    endpoint = os.getenv("PDF_OCR_ENDPOINT", "").strip()
    if not endpoint:
        return None
    headers = {"Content-Type": "application/pdf", "Accept": "application/json"}
    token = os.getenv("PDF_OCR_ACCESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, data=content, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=int(os.getenv("PDF_OCR_TIMEOUT_SECONDS", "90"))) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None
    text = result.get("text") if isinstance(result, dict) else None
    return str(text).strip() if text else None
