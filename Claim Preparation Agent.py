"""
Claim Preparation Agent for Velo Claim.

This is the production-shaped implementation of the first major Velo Claim
agent. It prepares a structured draft claim from FHIR-like source data.

Design:
    - LangGraph controls the workflow and state transitions.
    - The trigger is a trusted encounter from Velo Doctor or an RCM upload.
    - Patient, payer, provider, SOAP, documents, and charge lines are resolved
      from that encounter package or loaded from a FHIR REST endpoint.
    - MedGemma, served from your DGX, extracts structured clinical context.
    - Diagnosis and procedure codes are copied only from the encounter/FHIR
      source data; coding validation belongs to the next validation agent.
    - Pydantic validates every external-service output before the draft claim
      is built.
    - Audit logging is intentionally excluded for now, per the current request.
"""
 

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import b64encode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path
from pprint import pprint
from typing import Any, Literal, TypedDict
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError


def load_env_file(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs from a .env file without extra dependencies."""

    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = Path(__file__).resolve().parent / env_path
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


load_env_file()


# ---------------------------------------------------------------------------
# State and validated output models
# ---------------------------------------------------------------------------


ClaimPrepStatus = Literal[
    "CLAIM_PREPARATION_STARTED",
    "SUPERVISOR_ROUTED",
    "SOURCE_DATA_LOADED",
    "CLINICAL_CONTEXT_EXTRACTED",
    "CODES_EXTRACTED",
    "DRAFT_CLAIM_BUILT",
    "PREPARE_CLAIM_COMPLETED",
    "READY_FOR_VALIDATION",
    "CLAIM_PREPARATION_COMPLETED",
    "CLAIM_PREPARATION_FAILED",
]


class ClaimPreparationState(TypedDict, total=False):
    """Shared state that flows through the Claim Preparation Agent graph."""

    # Case routing context
    case_id: str
    claim_id: str
    encounter_id: str
    patient_id: str
    coverage_id: str
    source: str
    status: ClaimPrepStatus
    current_step: str
    next_agent: str
    trigger_source: str
    encounter_acceptance: dict[str, Any]

    # Runtime configuration: MedGemma clinical extraction
    use_medgemma: bool
    require_medgemma: bool
    medgemma_base_url: str
    medgemma_api_key: str
    medgemma_model: str
    medgemma_api_style: str
    medgemma_generate_path: str
    medgemma_used: bool

    fhir_base_url: str
    fhir_access_token: str
    fhir_auth_type: str
    fhir_token_url: str
    fhir_client_id: str
    fhir_client_secret: str
    fhir_client_auth_method: str
    fhir_scope: str
    fhir_audience: str
    fhir_private_key_path: str
    fhir_key_id: str
    fhir_jwks_url: str

    # Runtime configuration: jurisdiction-specific claim export
    claim_format: str
    jurisdiction: str

    # Source data. The preferred input is encounter_package. Individual
    # resources can still be injected by an API route or loaded via FHIR REST.
    encounter_package: dict[str, Any]
    uploaded_encounter: dict[str, Any]
    patient: dict[str, Any]
    coverage: dict[str, Any]
    encounter: dict[str, Any]
    provider: dict[str, Any]
    facility: dict[str, Any]
    conditions: list[dict[str, Any]]
    fhir_procedures: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    charge_items: list[dict[str, Any]]
    payer_rules: list[dict[str, Any]]

    # Agent outputs
    soap_fields: dict[str, Any]
    icd_codes: list[dict[str, Any]]
    procedure_codes: list[dict[str, Any]]
    cpt_codes: list[dict[str, Any]]
    cdt_codes: list[dict[str, Any]]
    code_links: list[dict[str, Any]]
    missing_fields: list[str]
    clinical_context: dict[str, Any]
    source_codes: dict[str, Any]
    claim_draft: dict[str, Any]
    claim: dict[str, Any]
    claim_payload: Any
    claim_payload_type: str
    formatted_claim: dict[str, Any]

    # Operational metadata
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


class DiagnosisCode(BaseModel):
    system: str = Field(default="ICD-10")
    code: str
    description: str
    type: str = Field(default="principal")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str
    requires_human_review: bool = True


class ProcedureCode(BaseModel):
    system: str = Field(default="CPT")
    code: str
    description: str
    units: int = Field(default=1, ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str
    requires_human_review: bool = True


class CodeLink(BaseModel):
    diagnosis_code: str
    procedure_code: str
    procedure_system: str = Field(default="CDT")
    relationship: str = Field(default="supports")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str
    requires_human_review: bool = True


class ClinicalContext(BaseModel):
    soap_note_excerpt: str
    chief_complaint: str | None = None
    encounter_type: str | None = None
    suspected_conditions: list[str] = Field(default_factory=list)
    documented_procedures: list[str] = Field(default_factory=list)
    planned_or_performed_services: list[str] = Field(default_factory=list)
    medical_necessity_summary: str | None = None
    documentation_gaps: list[str] = Field(default_factory=list)
    coding_relevant_facts: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    extraction_method: str


class SourceCodeExtractionResult(BaseModel):
    diagnosis_codes: list[DiagnosisCode] = Field(default_factory=list)
    procedure_codes: list[ProcedureCode] = Field(default_factory=list)
    code_links: list[CodeLink] = Field(default_factory=list)
    coding_warnings: list[str] = Field(default_factory=list)
    coding_method: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_human_review: bool = False
    human_review_reason: str = "Source-extracted claim codes must be validated before submission."


# ---------------------------------------------------------------------------
# Constants and helper functions
# ---------------------------------------------------------------------------


DEFAULT_MEDGEMMA_MODEL = "medgemma"
DEFAULT_MEDGEMMA_API_STYLE = "openai_chat"
DEFAULT_MEDGEMMA_GENERATE_PATH = "/generate"
DEFAULT_CLAIM_FORMAT = "AUTO"
DEFAULT_JURISDICTION = "AUTO"
DEFAULT_TRIGGER_SOURCE = "VELO_DOCTOR_APPROVED_ENCOUNTER"
DEFAULT_FHIR_AUTH_TYPE = "STATIC_BEARER"
DEFAULT_FHIR_CLIENT_AUTH_METHOD = "client_secret_basic"
DEFAULT_FHIR_SCOPE = "system/*.read"

TRIGGER_SOURCE_ALIASES = {
    "VELODOCTOR": "VELO_DOCTOR_APPROVED_ENCOUNTER",
    "VELODOCTORAPPROVEDENCOUNTER": "VELO_DOCTOR_APPROVED_ENCOUNTER",
    "APPROVEDENCOUNTER": "VELO_DOCTOR_APPROVED_ENCOUNTER",
    "RCM": "RCM_MANUAL_UPLOAD",
    "RCMMANUALUPLOAD": "RCM_MANUAL_UPLOAD",
    "MANUALUPLOAD": "RCM_MANUAL_UPLOAD",
}

CLAIM_FORMAT_ALIASES = {
    "AUTO": "AUTO",
    "CANONICAL": "CANONICAL",
    "INTERNAL": "CANONICAL",
    "NPHIES": "NPHIES",
    "KSA": "NPHIES",
    "SAUDI": "NPHIES",
    "SAUDIARABIA": "NPHIES",
    "SHAFAFIYA": "SHAFAFIYA",
    "SHAF": "SHAFAFIYA",
    "ABUDHABI": "SHAFAFIYA",
    "DOH": "SHAFAFIYA",
    "ECLAIMLINK": "ECLAIMLINK",
    "ECLAIM": "ECLAIMLINK",
    "DUBAI": "ECLAIMLINK",
    "DHA": "ECLAIMLINK",
}

JURISDICTION_ALIASES = {
    "AUTO": "AUTO",
    "KSA": "KSA",
    "SAUDI": "KSA",
    "SAUDIARABIA": "KSA",
    "ABUDHABI": "ABU_DHABI",
    "ABU_DHABI": "ABU_DHABI",
    "DOH": "ABU_DHABI",
    "DUBAI": "DUBAI",
    "DHA": "DUBAI",
}

def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def add_error(
    state: ClaimPreparationState,
    error_type: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        *state.get("errors", []),
        {
            "type": error_type,
            "message": message,
            "metadata": metadata or {},
            "timestamp": utc_now(),
        },
    ]


def add_warning(
    state: ClaimPreparationState,
    warning_type: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        *state.get("warnings", []),
        {
            "type": warning_type,
            "message": message,
            "metadata": metadata or {},
            "timestamp": utc_now(),
        },
    ]


def get_required(value: Any, field_name: str) -> Any:
    if value is None or value == "":
        raise ValueError(f"Missing required field: {field_name}")
    return value


def first_text(items: list[dict[str, Any]] | None, default: str | None = None) -> str | None:
    if not items:
        return default

    item = items[0]
    if item.get("text"):
        return item["text"]

    given = item.get("given", [])
    family = item.get("family")
    parts = [*given, family] if isinstance(given, list) else [given, family]
    text = " ".join(str(part) for part in parts if part)
    return text or default


def first_identifier(resource: dict[str, Any], *systems: str) -> str | None:
    identifiers = resource.get("identifier", [])
    for system in systems:
        for identifier in identifiers:
            if identifier.get("system") == system:
                return identifier.get("value")
    return identifiers[0].get("value") if identifiers else None


def first_codeable_text(codeable: dict[str, Any] | list[dict[str, Any]] | None) -> str | None:
    if not codeable:
        return None

    if isinstance(codeable, list):
        if not codeable:
            return None
        codeable = codeable[0]

    if codeable.get("text"):
        return codeable["text"]

    coding = codeable.get("coding", [])
    if coding:
        return coding[0].get("display") or coding[0].get("code")

    return None


def reference_id(reference: str | None) -> str | None:
    if not reference:
        return None
    return reference.split("/")[-1]


def reference_from_id(resource_type: str, value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        reference = value.get("reference")
        if reference:
            return str(reference)
        value = value.get("id") or value.get("value")
    if not value:
        return None
    text = str(value)
    return text if "/" in text else f"{resource_type}/{text}"


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize_token(value: str | None, default: str = "") -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or default).upper())


def normalize_trigger_source(value: str | None) -> str:
    token = normalize_token(value, DEFAULT_TRIGGER_SOURCE)
    return TRIGGER_SOURCE_ALIASES.get(token, value or DEFAULT_TRIGGER_SOURCE)


def resource_from_package(
    package: dict[str, Any],
    state: ClaimPreparationState,
    key: str,
) -> Any:
    value = state.get(key)
    if value:
        return value
    return package.get(key)


def encounter_from_state(state: ClaimPreparationState) -> dict[str, Any] | None:
    package = state.get("encounter_package") or {}
    encounter = state.get("encounter") or package.get("encounter")
    if encounter:
        return encounter
    uploaded = state.get("uploaded_encounter")
    return uploaded if isinstance(uploaded, dict) else None


def encounter_subject_patient_id(encounter: dict[str, Any]) -> str | None:
    subject = encounter.get("subject")
    if isinstance(subject, dict):
        return reference_id(subject.get("reference")) or subject.get("id")
    if isinstance(subject, str):
        return reference_id(subject)
    patient = encounter.get("patient")
    if isinstance(patient, dict):
        return reference_id(patient.get("reference")) or patient.get("id")
    if isinstance(patient, str):
        return reference_id(patient)
    return (
        encounter.get("patient_id")
        or encounter.get("patientId")
        or encounter.get("patientID")
    )


def encounter_coverage_id(encounter: dict[str, Any]) -> str | None:
    for key in ("coverage", "insurance"):
        value = encounter.get(key)
        if isinstance(value, list) and value:
            first_value = value[0]
            if isinstance(first_value, dict):
                reference = first_value.get("reference") or first_value.get("coverage", {}).get("reference")
                return reference_id(reference) or first_value.get("id")
        if isinstance(value, dict):
            return reference_id(value.get("reference")) or value.get("id")
        if isinstance(value, str):
            return reference_id(value)
    account = encounter.get("account")
    if isinstance(account, list) and account:
        first_account = account[0]
        if isinstance(first_account, dict):
            return reference_id(first_account.get("coverage", {}).get("reference"))
    return encounter.get("coverage_id") or encounter.get("coverageId")


def first_encounter_provider_reference(encounter: dict[str, Any]) -> str | None:
    participants = encounter.get("participant", [])
    for participant in participants:
        individual = participant.get("individual", {}) if isinstance(participant, dict) else {}
        reference = individual.get("reference")
        if reference:
            return reference

    provider = encounter.get("provider") or encounter.get("doctor") or encounter.get("clinician")
    reference = reference_from_id("Practitioner", provider)
    if reference:
        return reference

    for key in ("doctor_id", "doctorId", "provider_id", "providerId", "practitioner_id", "clinician_id"):
        reference = reference_from_id("Practitioner", encounter.get(key))
        if reference:
            return reference
    return None


def first_encounter_facility_reference(encounter: dict[str, Any]) -> str | None:
    service_provider = encounter.get("serviceProvider")
    if isinstance(service_provider, dict):
        return service_provider.get("reference")
    if isinstance(service_provider, str):
        return reference_from_id("Organization", service_provider)
    location = encounter.get("location", [])
    if isinstance(location, list) and location:
        location_ref = location[0].get("location", {}) if isinstance(location[0], dict) else {}
        reference = location_ref.get("reference") or reference_from_id("Location", location_ref)
        if reference:
            return reference
    facility = encounter.get("facility") or encounter.get("clinic") or encounter.get("organization")
    reference = reference_from_id("Organization", facility)
    if reference:
        return reference
    for key in ("facility_id", "facilityId", "clinic_id", "organization_id", "service_provider_id"):
        reference = reference_from_id("Organization", encounter.get(key))
        if reference:
            return reference
    return None


def encounter_acceptance_from_state(
    state: ClaimPreparationState,
    encounter: dict[str, Any] | None,
) -> dict[str, Any]:
    if state.get("encounter_acceptance"):
        return state["encounter_acceptance"]
    if state.get("encounter_approval"):
        return state["encounter_approval"]
    package = state.get("encounter_package") or {}
    if package.get("acceptance"):
        return package["acceptance"]
    if package.get("approval"):
        return package["approval"]
    if encounter:
        for key in ("acceptance", "approval", "doctor_approval", "velo_doctor_approval"):
            metadata = encounter.get(key)
            if isinstance(metadata, dict):
                return metadata
    return {}


def accepted_encounter_metadata(
    state: ClaimPreparationState,
    encounter: dict[str, Any] | None,
) -> dict[str, Any]:
    if not encounter:
        raise ValueError("Missing encounter. Provide encounter_package.encounter, encounter, or encounter_id with FHIR_BASE_URL.")

    metadata = encounter_acceptance_from_state(state, encounter)
    trigger_source = normalize_trigger_source(
        state.get("trigger_source") or metadata.get("source")
    )
    return {
        "status": metadata.get("status") or "received",
        "accepted": True,
        "accepted_by": (
            metadata.get("accepted_by")
            or metadata.get("approved_by")
            or metadata.get("doctor_id")
            or metadata.get("user_id")
        ),
        "accepted_at": (
            metadata.get("accepted_at")
            or metadata.get("approved_at")
            or metadata.get("timestamp")
            or utc_now()
        ),
        "source": metadata.get("source") or trigger_source,
        "approval_check_performed": False,
    }


def compact_json(value: Any, max_chars: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=True, indent=2, default=str)
    return text[:max_chars]


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse strict JSON, or recover a JSON object embedded in model text."""

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain a JSON object.")
    return json.loads(match.group(0))


def route_after_step(state: ClaimPreparationState) -> str:
    return "failed" if state.get("errors") else "continue"


def build_headers(
    *,
    bearer_token: str | None = None,
    accept: str = "application/json",
) -> dict[str, str]:
    headers = {"Accept": accept, "Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    return headers


def post_json(
    *,
    base_url: str,
    path: str,
    payload: dict[str, Any],
    bearer_token: str | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers=build_headers(bearer_token=bearer_token),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed with HTTP {exc.code}: {body}") from exc


def post_form(
    *,
    url: str,
    form: dict[str, str],
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed with HTTP {exc.code}: {body}") from exc


def base64url(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def resolve_local_path(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parent / candidate
    return candidate


def build_private_key_jwt_assertion(
    *,
    token_url: str,
    client_id: str,
    private_key_path: str,
    key_id: str,
    jwks_url: str | None = None,
    audience: str | None = None,
) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = {
        "alg": "RS384",
        "typ": "JWT",
        "kid": key_id,
    }
    if jwks_url:
        header["jku"] = jwks_url
    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": audience or token_url,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "iat": now,
        "exp": now + 300,
    }

    signing_input = ".".join(
        [
            base64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    ).encode("ascii")

    private_key = serialization.load_pem_private_key(
        resolve_local_path(private_key_path).read_bytes(),
        password=None,
    )
    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA384(),
    )
    return f"{signing_input.decode('ascii')}.{base64url(signature)}"


def fhir_backend_access_token(state: ClaimPreparationState) -> str | None:
    static_token = state.get("fhir_access_token") or os.getenv("FHIR_ACCESS_TOKEN")
    auth_type = (
        state.get("fhir_auth_type")
        or os.getenv("FHIR_AUTH_TYPE")
        or DEFAULT_FHIR_AUTH_TYPE
    ).strip().lower()

    if static_token:
        return static_token
    if auth_type in {"", "none", "no_auth"}:
        return None
    if auth_type not in {
        "client_credentials",
        "backend",
        "backend_services",
        "backend_services_jwt",
        "private_key_jwt",
    }:
        return None

    token_url = state.get("fhir_token_url") or os.getenv("FHIR_TOKEN_URL")
    client_id = state.get("fhir_client_id") or os.getenv("FHIR_CLIENT_ID")
    client_secret = state.get("fhir_client_secret") or os.getenv("FHIR_CLIENT_SECRET")
    client_auth_method = (
        state.get("fhir_client_auth_method")
        or os.getenv("FHIR_CLIENT_AUTH_METHOD")
        or DEFAULT_FHIR_CLIENT_AUTH_METHOD
    ).strip().lower()
    scope = state.get("fhir_scope") or os.getenv("FHIR_SCOPE") or DEFAULT_FHIR_SCOPE
    audience = state.get("fhir_audience") or os.getenv("FHIR_AUDIENCE")
    private_key_path = state.get("fhir_private_key_path") or os.getenv("FHIR_PRIVATE_KEY_PATH")
    key_id = state.get("fhir_key_id") or os.getenv("FHIR_KEY_ID")
    jwks_url = state.get("fhir_jwks_url") or os.getenv("FHIR_JWKS_URL")

    if auth_type in {"backend_services_jwt", "private_key_jwt"}:
        client_auth_method = "private_key_jwt"

    if not token_url:
        raise RuntimeError("FHIR backend auth requires FHIR_TOKEN_URL.")
    if not client_id:
        raise RuntimeError("FHIR backend auth requires FHIR_CLIENT_ID.")

    form = {
        "grant_type": "client_credentials",
        "scope": scope,
    }

    headers: dict[str, str] = {}
    if client_auth_method == "client_secret_basic":
        if not client_secret:
            raise RuntimeError("FHIR_CLIENT_AUTH_METHOD=client_secret_basic requires FHIR_CLIENT_SECRET.")
        credentials = f"{client_id}:{client_secret}".encode("utf-8")
        headers["Authorization"] = f"Basic {b64encode(credentials).decode('ascii')}"
        if audience:
            form["aud"] = audience
    elif client_auth_method == "client_secret_post":
        if not client_secret:
            raise RuntimeError("FHIR_CLIENT_AUTH_METHOD=client_secret_post requires FHIR_CLIENT_SECRET.")
        form["client_id"] = client_id
        form["client_secret"] = client_secret
        if audience:
            form["aud"] = audience
    elif client_auth_method == "private_key_jwt":
        if not private_key_path:
            raise RuntimeError("FHIR_CLIENT_AUTH_METHOD=private_key_jwt requires FHIR_PRIVATE_KEY_PATH.")
        if not key_id:
            raise RuntimeError("FHIR_CLIENT_AUTH_METHOD=private_key_jwt requires FHIR_KEY_ID.")
        form["client_assertion_type"] = (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        form["client_assertion"] = build_private_key_jwt_assertion(
            token_url=token_url,
            client_id=client_id,
            private_key_path=private_key_path,
            key_id=key_id,
            jwks_url=jwks_url,
            audience=audience or token_url,
        )
    else:
        raise RuntimeError(
            "Unsupported FHIR_CLIENT_AUTH_METHOD. Use client_secret_basic, "
            "client_secret_post, or private_key_jwt."
        )

    token_response = post_form(url=token_url, form=form, headers=headers)
    access_token = token_response.get("access_token")
    if not access_token:
        raise RuntimeError("FHIR token response did not include access_token.")
    return str(access_token)


def response_text_from_openai_chat(data: dict[str, Any]) -> str:
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("MedGemma response did not include choices.")
    message = choices[0].get("message", {})
    content = message.get("content")
    if not content:
        raise ValueError("MedGemma response did not include message content.")
    return str(content)


def response_text_from_generate(data: dict[str, Any]) -> str:
    for key in ("generated_text", "text", "response", "output"):
        value = data.get(key)
        if isinstance(value, str):
            return value

    if isinstance(data.get("outputs"), list) and data["outputs"]:
        first = data["outputs"][0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return str(first.get("text") or first.get("generated_text") or first)

    raise ValueError("MedGemma generate response did not include text output.")


def normalize_fhir_document_reference(document: dict[str, Any]) -> dict[str, Any]:
    content = document.get("content", [])
    attachment = content[0].get("attachment", {}) if content else {}
    return {
        "id": document.get("id"),
        "name": attachment.get("title") or document.get("description") or document.get("id"),
        "type": first_codeable_text(document.get("type")) or "clinical_document",
        "status": document.get("status"),
        "url": attachment.get("url"),
        "content_type": attachment.get("contentType"),
    }


def normalize_fhir_charge_item(charge_item: dict[str, Any]) -> dict[str, Any]:
    codings = codeable_codings(charge_item.get("code"))
    coding = codings[0] if codings else {}
    price = charge_item.get("priceOverride") or {}
    return {
        "id": charge_item.get("id"),
        "description": (
            coding.get("display")
            or first_codeable_text(charge_item.get("code"))
            or charge_item.get("description")
            or charge_item.get("id")
        ),
        "code": coding.get("code"),
        "system": normalize_code_system(coding.get("system"), "CPT") if coding else None,
        "amount": price.get("value"),
        "currency": price.get("currency"),
        "quantity": charge_item.get("quantity"),
        "start": charge_item.get("occurrenceDateTime"),
    }


def normalize_encounter_attachment(item: dict[str, Any], default_type: str) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": (
            item.get("name")
            or item.get("title")
            or item.get("file_name")
            or item.get("filename")
            or item.get("id")
        ),
        "type": item.get("type") or default_type,
        "status": item.get("status") or "available",
        "url": item.get("url") or item.get("path"),
        "content_type": item.get("content_type") or item.get("contentType"),
    }


def collect_encounter_attachments(encounter: dict[str, Any]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for key, default_type in (
        ("attachments", "clinical_document"),
        ("documents", "clinical_document"),
        ("radiology", "radiology_report"),
        ("radiology_reports", "radiology_report"),
        ("images", "clinical_image"),
        ("radio_images", "radiology_image"),
        ("xray_images", "radiology_image"),
        ("invoices", "invoice"),
        ("pharmacy_invoices", "pharmacy_invoice"),
        ("pharma_invoices", "pharmacy_invoice"),
    ):
        values = encounter.get(key) or []
        if isinstance(values, dict):
            values = [values]
        for item in values:
            if isinstance(item, dict):
                collected.append(normalize_encounter_attachment(item, default_type))
    return collected


def normalize_encounter_charge_item(item: dict[str, Any], default_system: str = "CPT") -> dict[str, Any]:
    return {
        "id": item.get("id") or item.get("invoice_id") or item.get("line_id"),
        "description": item.get("description") or item.get("service") or item.get("name"),
        "code": item.get("code") or item.get("serviceCode") or item.get("drug_code"),
        "system": item.get("system") or item.get("codeSystem") or default_system,
        "amount": item.get("amount") or item.get("net") or item.get("gross"),
        "currency": item.get("currency") or "AED",
        "quantity": item.get("quantity") or item.get("units") or 1,
        "start": item.get("start") or item.get("service_date"),
    }


def collect_encounter_charge_items(encounter: dict[str, Any]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for key, default_system in (
        ("charge_items", "CPT"),
        ("activities", "CPT"),
        ("invoice_lines", "CPT"),
        ("pharmacy_items", "DDC"),
        ("pharma_items", "DDC"),
        ("medications", "DDC"),
    ):
        values = encounter.get(key) or []
        if isinstance(values, dict):
            values = [values]
        for item in values:
            if isinstance(item, dict):
                collected.append(normalize_encounter_charge_item(item, default_system))

    for invoice_key in ("invoices", "pharmacy_invoices", "pharma_invoices"):
        invoices = encounter.get(invoice_key) or []
        if isinstance(invoices, dict):
            invoices = [invoices]
        for invoice in invoices:
            if not isinstance(invoice, dict):
                continue
            lines = invoice.get("lines") or invoice.get("items") or []
            if isinstance(lines, dict):
                lines = [lines]
            for line in lines:
                if isinstance(line, dict):
                    default_system = "DDC" if "pharma" in invoice_key or "pharmacy" in invoice_key else "CPT"
                    collected.append(normalize_encounter_charge_item(line, default_system))
    return collected


# ---------------------------------------------------------------------------
# FHIR source loading
# ---------------------------------------------------------------------------


class FHIRClient:
    """Small FHIR R4 REST client for reading resources needed by preparation."""

    def __init__(self, base_url: str, access_token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token

    def _request_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params or {})}" if params else ""
        url = f"{self.base_url}/{path.lstrip('/')}{query}"
        headers = {"Accept": "application/fhir+json, application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"FHIR request failed {exc.code} for {url}: {body}") from exc

    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        return self._request_json(f"{resource_type}/{resource_id}")

    def search_first(self, resource_type: str, params: dict[str, str]) -> dict[str, Any] | None:
        bundle = self._request_json(resource_type, params=params)
        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if resource:
                return resource
        return None

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]:
        bundle = self._request_json(resource_type, params=params)
        return [
            entry["resource"]
            for entry in bundle.get("entry", [])
            if isinstance(entry.get("resource"), dict)
        ]


def get_fhir_client(state: ClaimPreparationState) -> FHIRClient | None:
    base_url = state.get("fhir_base_url") or os.getenv("FHIR_BASE_URL")
    if not base_url:
        return None
    token = fhir_backend_access_token(state)
    return FHIRClient(base_url=base_url, access_token=token)


def resolve_provider_and_facility(
    encounter: dict[str, Any],
    fhir_client: FHIRClient | None,
    provider: dict[str, Any] | None,
    facility: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if provider and facility:
        return provider, facility

    if not fhir_client:
        return provider, facility

    provider_ref = first_encounter_provider_reference(encounter)
    facility_ref = first_encounter_facility_reference(encounter)

    if not provider and provider_ref and "/" in provider_ref:
        resource_type, resource_id = provider_ref.split("/", 1)
        provider = fhir_client.read(resource_type, resource_id)

    if not facility and facility_ref and "/" in facility_ref:
        resource_type, resource_id = facility_ref.split("/", 1)
        facility = fhir_client.read(resource_type, resource_id)

    return provider, facility


def load_source_data(state: ClaimPreparationState) -> ClaimPreparationState:
    """Load source resources from a trusted encounter package or FHIR."""

    try:
        package = state.get("encounter_package") or {}
        fhir_client = get_fhir_client(state)
        encounter = encounter_from_state(state)

        encounter_id = state.get("encounter_id") or package.get("encounter_id")
        if not encounter and fhir_client:
            encounter = fhir_client.read("Encounter", get_required(encounter_id, "encounter_id"))
        encounter_data = encounter or {}

        acceptance = accepted_encounter_metadata(state, encounter)
        trigger_source = normalize_trigger_source(state.get("trigger_source") or acceptance.get("source"))

        patient = resource_from_package(package, state, "patient")
        coverage = resource_from_package(package, state, "coverage")
        provider = resource_from_package(package, state, "provider")
        facility = resource_from_package(package, state, "facility")
        conditions = resource_from_package(package, state, "conditions")
        fhir_procedures = resource_from_package(package, state, "fhir_procedures")
        attachments = resource_from_package(package, state, "attachments")
        charge_items = resource_from_package(package, state, "charge_items")
        if conditions is None:
            conditions = (
                package.get("diagnoses")
                or encounter_data.get("conditions")
                or encounter_data.get("diagnoses")
                or encounter_data.get("diagnosis")
            )
        if fhir_procedures is None:
            fhir_procedures = (
                package.get("procedures")
                or encounter_data.get("procedures")
                or encounter_data.get("procedure")
            )
        if attachments is None:
            attachments = package.get("documents") or collect_encounter_attachments(encounter_data)
        if charge_items is None:
            charge_items = package.get("activities") or collect_encounter_charge_items(encounter_data)

        patient_id = (
            state.get("patient_id")
            or package.get("patient_id")
            or encounter_subject_patient_id(encounter)
        )
        coverage_id = (
            state.get("coverage_id")
            or package.get("coverage_id")
            or encounter_coverage_id(encounter)
        )

        if not patient and fhir_client:
            patient = fhir_client.read("Patient", get_required(patient_id, "patient_id from encounter"))

        if not coverage and fhir_client and coverage_id:
            coverage = fhir_client.read("Coverage", coverage_id)

        if not coverage and fhir_client:
            patient_ref = f"Patient/{get_required(patient_id or (patient or {}).get('id'), 'patient_id')}"
            coverage = fhir_client.search_first("Coverage", {"beneficiary": patient_ref})

        if encounter:
            provider, facility = resolve_provider_and_facility(
                encounter=encounter,
                fhir_client=fhir_client,
                provider=provider,
                facility=facility,
            )

        if fhir_client and patient:
            patient_ref = f"Patient/{patient.get('id')}"
            encounter_ref = f"Encounter/{encounter.get('id')}" if encounter else None

            if conditions is None:
                condition_params = {"subject": patient_ref}
                if encounter_ref:
                    condition_params["encounter"] = encounter_ref
                conditions = fhir_client.search("Condition", condition_params)

            if fhir_procedures is None:
                procedure_params = {"subject": patient_ref}
                if encounter_ref:
                    procedure_params["encounter"] = encounter_ref
                fhir_procedures = fhir_client.search("Procedure", procedure_params)

            if attachments is None:
                document_params = {"subject": patient_ref}
                if encounter_ref:
                    document_params["encounter"] = encounter_ref
                attachments = [
                    normalize_fhir_document_reference(document)
                    for document in fhir_client.search("DocumentReference", document_params)
                ]

            if charge_items is None:
                charge_params = {"subject": patient_ref}
                if encounter_ref:
                    charge_params["context"] = encounter_ref
                charge_items = [
                    normalize_fhir_charge_item(charge_item)
                    for charge_item in fhir_client.search("ChargeItem", charge_params)
                ]

        missing = [
            name
            for name, value in {
                "patient": patient,
                "coverage": coverage,
                "encounter": encounter,
                "provider": provider,
                "facility": facility,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(
                "Missing required source resource(s): "
                + ", ".join(missing)
                + ". Include them in encounter_package or configure FHIR_BASE_URL."
            )

        attachments = attachments or encounter.get("attachments") or encounter.get("documents") or []
        charge_items = charge_items or encounter.get("charge_items") or encounter.get("activities") or []

        return {
            **state,
            "trigger_source": trigger_source,
            "encounter_acceptance": acceptance,
            "encounter_package": package,
            "patient": patient,
            "coverage": coverage,
            "encounter": encounter,
            "encounter_id": encounter.get("id") or encounter_id,
            "patient_id": patient.get("id") or patient_id,
            "coverage_id": coverage.get("id") or coverage_id,
            "provider": provider,
            "facility": facility,
            "conditions": as_list(conditions),
            "fhir_procedures": as_list(fhir_procedures),
            "attachments": as_list(attachments),
            "charge_items": as_list(charge_items),
            "status": "SOURCE_DATA_LOADED",
        }
    except Exception as exc:
        return {
            **state,
            "status": "CLAIM_PREPARATION_FAILED",
            "errors": add_error(state, "SOURCE_DATA_LOAD_FAILED", str(exc)),
        }


# ---------------------------------------------------------------------------
# MedGemma clinical extraction
# ---------------------------------------------------------------------------


class MedGemmaClinicalLLM:
    """HTTP adapter for MedGemma served from the DGX."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        api_style: str | None = None,
        generate_path: str | None = None,
    ) -> None:
        self.base_url = base_url or os.getenv("MEDGEMMA_BASE_URL")
        self.api_key = api_key or os.getenv("MEDGEMMA_API_KEY")
        self.model = model or os.getenv("MEDGEMMA_MODEL") or DEFAULT_MEDGEMMA_MODEL
        self.api_style = (
            api_style or os.getenv("MEDGEMMA_API_STYLE") or DEFAULT_MEDGEMMA_API_STYLE
        ).lower()
        self.generate_path = (
            generate_path
            or os.getenv("MEDGEMMA_GENERATE_PATH")
            or DEFAULT_MEDGEMMA_GENERATE_PATH
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _prompt(self, *, instructions: str, payload: dict[str, Any]) -> str:
        return (
            f"{instructions.strip()}\n\n"
            "Return JSON only. Do not include markdown fences or commentary.\n\n"
            "INPUT:\n"
            f"{compact_json(payload)}"
        )

    def _json_response(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
        max_output_tokens: int = 1800,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("MEDGEMMA_BASE_URL is not configured.")

        prompt = self._prompt(instructions=instructions, payload=payload)

        if self.api_style == "openai_chat":
            data = post_json(
                base_url=self.base_url,
                path="/v1/chat/completions",
                bearer_token=self.api_key,
                timeout_seconds=90,
                payload={
                    "model": self.model,
                    "temperature": 0,
                    "max_tokens": max_output_tokens,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are MedGemma running inside Velo Claim.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            return extract_json_object(response_text_from_openai_chat(data))

        if self.api_style == "generate":
            data = post_json(
                base_url=self.base_url,
                path=self.generate_path,
                bearer_token=self.api_key,
                timeout_seconds=90,
                payload={
                    "model": self.model,
                    "inputs": prompt,
                    "parameters": {
                        "temperature": 0,
                        "max_new_tokens": max_output_tokens,
                    },
                },
            )
            return extract_json_object(response_text_from_generate(data))

        raise ValueError(
            f"Unsupported MEDGEMMA_API_STYLE={self.api_style!r}. "
            "Use openai_chat or generate."
        )

    def extract_clinical_context(
        self,
        *,
        patient: dict[str, Any],
        encounter: dict[str, Any],
        conditions: list[dict[str, Any]],
        fhir_procedures: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        charge_items: list[dict[str, Any]],
    ) -> ClinicalContext:
        instructions = """
You are a clinical information extraction specialist for insurance claim preparation.

Extract only facts supported by the encounter and clinical note. Do not assign,
suggest, validate, or correct ICD/CDT/CPT/HCPCS codes. Diagnosis and procedure
codes are copied from the encounter/FHIR source data elsewhere in this agent,
then validated later by the Claim Validation Agent.

Respond with one JSON object using exactly this shape:
{
  "soap_note_excerpt": "short excerpt of the relevant note",
  "chief_complaint": "string or null",
  "encounter_type": "string or null",
  "suspected_conditions": ["condition strings"],
  "documented_procedures": ["documented procedure strings"],
  "planned_or_performed_services": ["service strings"],
  "medical_necessity_summary": "one concise sentence or null",
  "documentation_gaps": ["missing or weak document names"],
  "coding_relevant_facts": ["facts useful for later claim validation"],
  "confidence": 0.0,
  "extraction_method": "medgemma_dgx"
}
"""
        payload = {
            "patient": patient,
            "encounter": encounter,
            "conditions": conditions,
            "procedures": fhir_procedures,
            "attachments": attachments,
            "charge_items": charge_items,
        }
        data = self._json_response(instructions=instructions, payload=payload)
        data["extraction_method"] = "medgemma_dgx"
        return ClinicalContext.model_validate(data)

def get_medgemma(state: ClaimPreparationState) -> MedGemmaClinicalLLM:
    return MedGemmaClinicalLLM(
        base_url=state.get("medgemma_base_url"),
        api_key=state.get("medgemma_api_key"),
        model=state.get("medgemma_model"),
        api_style=state.get("medgemma_api_style"),
        generate_path=state.get("medgemma_generate_path"),
    )


# ---------------------------------------------------------------------------
# Claim Preparation Agent internal components
# ---------------------------------------------------------------------------


class FHIRReaderTool:
    """Internal tool: fetch and normalize EHR/FHIR source data."""

    def run(self, state: ClaimPreparationState) -> ClaimPreparationState:
        return load_source_data(state)


class SOAPExtractorChain:
    """Internal chain: PromptTemplate -> MedGemma -> Pydantic parser."""

    def invoke(self, state: ClaimPreparationState) -> ClaimPreparationState:
        medgemma = get_medgemma(state)
        use_medgemma = state.get("use_medgemma", True)
        require_medgemma = state.get("require_medgemma", False)

        if use_medgemma and medgemma.is_configured:
            clinical_context = medgemma.extract_clinical_context(
                patient=state["patient"],
                encounter=state["encounter"],
                conditions=state.get("conditions", []),
                fhir_procedures=state.get("fhir_procedures", []),
                attachments=state.get("attachments", []),
                charge_items=state.get("charge_items", []),
            )
            return {
                **state,
                "soap_fields": clinical_context.model_dump(),
                "clinical_context": clinical_context.model_dump(),
                "medgemma_used": True,
                "status": "CLINICAL_CONTEXT_EXTRACTED",
            }

        if require_medgemma:
            raise RuntimeError("MedGemma is required, but MEDGEMMA_BASE_URL is not configured.")

        clinical_context = local_extract_clinical_context(
            encounter=state["encounter"],
            conditions=state.get("conditions", []),
            fhir_procedures=state.get("fhir_procedures", []),
            attachments=state.get("attachments", []),
            charge_items=state.get("charge_items", []),
        )
        warnings = state.get("warnings", [])
        if use_medgemma and not medgemma.is_configured:
            warnings = add_warning(
                state,
                "MEDGEMMA_NOT_CONFIGURED",
                "MEDGEMMA_BASE_URL is not configured; used local clinical extraction fallback.",
                {"medgemma_model": medgemma.model},
            )
        if clinical_context.documentation_gaps:
            warnings = [
                *warnings,
                {
                    "type": "DOCUMENTATION_GAP",
                    "message": "Clinical context extraction found possible missing documentation.",
                    "metadata": {"documentation_gaps": clinical_context.documentation_gaps},
                    "timestamp": utc_now(),
                },
            ]

        return {
            **state,
            "soap_fields": clinical_context.model_dump(),
            "clinical_context": clinical_context.model_dump(),
            "warnings": warnings,
            "status": "CLINICAL_CONTEXT_EXTRACTED",
        }


def normalize_code_system(system: Any, default: str) -> str:
    token = normalize_token(str(system or ""), default)
    if "ICD10" in token or token == "ICD":
        return "ICD-10"
    if "SNOMED" in token:
        return "SNOMED-CT"
    if "HCPCS" in token:
        return "HCPCS"
    if "CPT" in token or "AMA" in token:
        return "CPT"
    if "CDT" in token or "ADA" in token:
        return "CDT"
    if "DDC" in token:
        return "DDC"
    if not system:
        return default
    return str(system).upper()


def codeable_codings(codeable: Any) -> list[dict[str, Any]]:
    if not codeable:
        return []

    if isinstance(codeable, list):
        return [
            coding
            for item in codeable
            for coding in codeable_codings(item)
        ]

    if not isinstance(codeable, dict):
        return []

    codings: list[dict[str, Any]] = []
    for coding in codeable.get("coding", []):
        if isinstance(coding, dict):
            normalized = dict(coding)
            normalized.setdefault("display", codeable.get("text"))
            codings.append(normalized)

    direct_code = codeable.get("code")
    if direct_code:
        codings.append(
            {
                "system": codeable.get("system") or codeable.get("codeSystem"),
                "code": direct_code,
                "display": codeable.get("display") or codeable.get("text"),
            }
        )

    return codings


def diagnosis_type_from_source(source: dict[str, Any], default: str = "principal") -> str:
    for key in ("type", "diagnosis_type", "diagnosisType"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
        text = first_codeable_text(value) if isinstance(value, (dict, list)) else None
        if text:
            return text
    return default


def add_diagnosis_from_codeable(
    *,
    diagnoses: dict[tuple[str, str], DiagnosisCode],
    codeable: Any,
    evidence: str,
    diagnosis_type: str = "principal",
) -> None:
    for coding in codeable_codings(codeable):
        code = coding.get("code")
        if not code:
            continue
        system = normalize_code_system(
            coding.get("system") or coding.get("code_system"),
            "ICD-10",
        )
        key = (system, str(code))
        diagnoses.setdefault(
            key,
            DiagnosisCode(
                system=system,
                code=str(code),
                description=str(coding.get("display") or coding.get("description") or code),
                type=diagnosis_type,
                confidence=1.0,
                evidence=evidence,
                requires_human_review=False,
            ),
        )


def add_procedure_from_codeable(
    *,
    procedures: dict[tuple[str, str], ProcedureCode],
    codeable: Any,
    evidence: str,
    units: int = 1,
    default_system: str = "CPT",
) -> None:
    for coding in codeable_codings(codeable):
        code = coding.get("code")
        if not code:
            continue
        system = normalize_code_system(
            coding.get("system") or coding.get("code_system"),
            default_system,
        )
        key = (system, str(code))
        procedures.setdefault(
            key,
            ProcedureCode(
                system=system,
                code=str(code),
                description=str(coding.get("display") or coding.get("description") or code),
                units=units,
                confidence=1.0,
                evidence=evidence,
                requires_human_review=False,
            ),
        )


def extract_encounter_diagnoses(
    encounter: dict[str, Any],
    diagnoses: dict[tuple[str, str], DiagnosisCode],
) -> None:
    diagnosis_items = encounter.get("diagnosis") or encounter.get("diagnoses") or []
    if isinstance(diagnosis_items, dict):
        diagnosis_items = [diagnosis_items]

    for item in diagnosis_items:
        if not isinstance(item, dict):
            continue
        diagnosis_type = diagnosis_type_from_source(item)
        for key in ("diagnosisCodeableConcept", "diagnosisCode", "code", "condition"):
            value = item.get(key)
            if isinstance(value, str):
                value = {
                    "code": value,
                    "system": item.get("system") or item.get("codeSystem"),
                    "display": item.get("description") or item.get("display"),
                }
            if isinstance(value, dict) and (value.get("coding") or value.get("code")):
                add_diagnosis_from_codeable(
                    diagnoses=diagnoses,
                    codeable=value,
                    evidence="Extracted from encounter diagnosis.",
                    diagnosis_type=diagnosis_type,
                )

    for key in ("diagnosisCodeableConcept", "diagnosis_code", "diagnosisCode"):
        value = encounter.get(key)
        if isinstance(value, str):
            value = {
                "code": value,
                "system": encounter.get("diagnosis_system") or encounter.get("diagnosisSystem"),
                "display": encounter.get("diagnosis_description") or encounter.get("diagnosisDescription"),
            }
        add_diagnosis_from_codeable(
            diagnoses=diagnoses,
            codeable=value,
            evidence="Extracted from encounter diagnosis field.",
        )


def extract_condition_diagnoses(
    conditions: list[dict[str, Any]],
    diagnoses: dict[tuple[str, str], DiagnosisCode],
) -> None:
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        evidence = f"Extracted from FHIR Condition/{condition.get('id', 'unknown')}."
        codeable = (
            condition.get("code")
            or condition.get("diagnosisCodeableConcept")
            or condition.get("diagnosisCode")
            or condition.get("diagnosis_code")
            or condition.get("diagnosis")
        )
        if isinstance(codeable, str):
            codeable = {
                "code": codeable,
                "system": condition.get("system") or condition.get("codeSystem"),
                "display": condition.get("description") or condition.get("display"),
            }
        add_diagnosis_from_codeable(
            diagnoses=diagnoses,
            codeable=codeable,
            evidence=evidence,
            diagnosis_type=diagnosis_type_from_source(condition),
        )


def extract_fhir_procedures(
    fhir_procedures: list[dict[str, Any]],
    procedures: dict[tuple[str, str], ProcedureCode],
) -> None:
    for procedure in fhir_procedures:
        if not isinstance(procedure, dict):
            continue
        evidence = f"Extracted from FHIR Procedure/{procedure.get('id', 'unknown')}."
        codeable = (
            procedure.get("code")
            or procedure.get("procedureCodeableConcept")
            or procedure.get("procedureCode")
            or procedure.get("procedure_code")
        )
        if isinstance(codeable, str):
            codeable = {
                "code": codeable,
                "system": procedure.get("system") or procedure.get("codeSystem"),
                "display": procedure.get("description") or procedure.get("display"),
            }
        add_procedure_from_codeable(
            procedures=procedures,
            codeable=codeable,
            evidence=evidence,
            units=quantity_value(procedure),
        )


def extract_encounter_procedures(
    encounter: dict[str, Any],
    procedures: dict[tuple[str, str], ProcedureCode],
) -> None:
    procedure_items = encounter.get("procedure") or encounter.get("procedures") or []
    if isinstance(procedure_items, dict):
        procedure_items = [procedure_items]

    for item in procedure_items:
        if not isinstance(item, dict):
            continue
        codeable = (
            item.get("procedureCodeableConcept")
            or item.get("procedureCode")
            or item.get("code")
        )
        if isinstance(codeable, str):
            codeable = {
                "code": codeable,
                "system": item.get("system") or item.get("codeSystem"),
                "display": item.get("description") or item.get("display"),
            }
        add_procedure_from_codeable(
            procedures=procedures,
            codeable=codeable,
            evidence="Extracted from encounter procedure.",
            units=quantity_value(item),
        )


def extract_charge_item_procedures(
    charge_items: list[dict[str, Any]],
    procedures: dict[tuple[str, str], ProcedureCode],
) -> None:
    for item in charge_items:
        if not isinstance(item, dict):
            continue

        codeable = item.get("productOrService") or item.get("service")
        if not codeable and (item.get("code") or item.get("serviceCode")):
            codeable = {
                "coding": [
                    {
                        "system": item.get("system") or item.get("codeSystem"),
                        "code": item.get("code") or item.get("serviceCode"),
                        "display": item.get("description"),
                    }
                ],
                "text": item.get("description"),
            }

        add_procedure_from_codeable(
            procedures=procedures,
            codeable=codeable,
            evidence=f"Extracted from charge item/{item.get('id', 'unknown')}.",
            units=quantity_value(item),
        )


def extract_source_code_result(state: ClaimPreparationState) -> SourceCodeExtractionResult:
    diagnoses: dict[tuple[str, str], DiagnosisCode] = {}
    procedures: dict[tuple[str, str], ProcedureCode] = {}

    extract_condition_diagnoses(state.get("conditions", []), diagnoses)
    extract_encounter_diagnoses(state["encounter"], diagnoses)
    extract_fhir_procedures(state.get("fhir_procedures", []), procedures)
    extract_encounter_procedures(state["encounter"], procedures)
    extract_charge_item_procedures(state.get("charge_items", []), procedures)

    diagnosis_codes = list(diagnoses.values())
    procedure_codes = list(procedures.values())
    warnings = []
    if not diagnosis_codes:
        warnings.append("No diagnosis code was present in the encounter or FHIR Condition resources.")
    if not procedure_codes:
        warnings.append("No procedure code was present in the encounter, FHIR Procedure resources, or charge items.")

    confidence_values = [
        item.confidence
        for item in [*diagnosis_codes, *procedure_codes]
    ]
    return SourceCodeExtractionResult(
        diagnosis_codes=diagnosis_codes,
        procedure_codes=procedure_codes,
        code_links=[],
        coding_warnings=warnings,
        coding_method="source_encounter_fhir_extraction",
        confidence=min(confidence_values) if confidence_values else 0.0,
        requires_human_review=False,
        human_review_reason="Source-extracted claim codes must be validated by the Claim Validation Agent before submission.",
    )


class SourceCodeExtractorChain:
    """Internal chain: encounter/FHIR source data -> source code container."""

    def invoke(self, state: ClaimPreparationState) -> ClaimPreparationState:
        ClinicalContext.model_validate(state["soap_fields"])
        source_codes = extract_source_code_result(state)
        warnings = state.get("warnings", [])
        for warning in source_codes.coding_warnings:
            warnings = [
                *warnings,
                {
                    "type": "SOURCE_CODE_WARNING",
                    "message": warning,
                    "metadata": {},
                    "timestamp": utc_now(),
                },
            ]

        return self._with_code_fields(
            state={**state, "warnings": warnings},
            source_codes=source_codes,
        )

    @staticmethod
    def _with_code_fields(
        *,
        state: ClaimPreparationState,
        source_codes: SourceCodeExtractionResult,
    ) -> ClaimPreparationState:
        procedure_codes = [item.model_dump() for item in source_codes.procedure_codes]
        return {
            **state,
            "source_codes": source_codes.model_dump(),
            "icd_codes": [item.model_dump() for item in source_codes.diagnosis_codes],
            "procedure_codes": procedure_codes,
            "code_links": [item.model_dump() for item in source_codes.code_links],
            "cpt_codes": [
                item for item in procedure_codes if item.get("system", "").upper() == "CPT"
            ],
            "cdt_codes": [
                item for item in procedure_codes if item.get("system", "").upper() == "CDT"
            ],
            "status": "CODES_EXTRACTED",
        }


class ClaimBuilder:
    """Internal pure-Python builder: assemble ClaimDraft from all prior results."""

    def run(self, state: ClaimPreparationState) -> ClaimPreparationState:
        clinical_context = ClinicalContext.model_validate(state["soap_fields"])
        source_codes = SourceCodeExtractionResult.model_validate(state["source_codes"])

        claim = build_draft_claim(
            case_id=state["case_id"],
            patient=state["patient"],
            coverage=state["coverage"],
            encounter=state["encounter"],
            provider=state["provider"],
            facility=state["facility"],
            attachments=state.get("attachments", []),
            charge_items=state.get("charge_items", []),
            clinical_context=clinical_context,
            source_codes=source_codes,
            payer_rules=state.get("payer_rules", []),
            claim_format=state.get("claim_format"),
            jurisdiction=state.get("jurisdiction"),
            trigger_source=state.get("trigger_source"),
            encounter_acceptance=state.get("encounter_acceptance"),
        )

        return {
            **state,
            "claim": claim,
            "claim_draft": claim,
            "claim_id": claim["claim_id"],
            "claim_format": claim["submission"]["format"],
            "jurisdiction": claim["submission"]["jurisdiction"],
            "claim_payload": claim["submission"]["payload"],
            "claim_payload_type": claim["submission"]["payload_type"],
            "formatted_claim": claim["submission"],
            "status": "DRAFT_CLAIM_BUILT",
        }


fhir_reader = FHIRReaderTool()
soap_chain = SOAPExtractorChain()
code_chain = SourceCodeExtractorChain()
claim_builder = ClaimBuilder()


# ---------------------------------------------------------------------------
# Local fallback clinical extraction
# ---------------------------------------------------------------------------


def encounter_note_text(encounter: dict[str, Any]) -> str:
    for key in ("soap_note", "clinical_note", "note", "reasonCodeText"):
        value = encounter.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    reason_code = first_codeable_text(encounter.get("reasonCode"))
    encounter_type = first_codeable_text(encounter.get("type"))
    parts = [part for part in [reason_code, encounter_type] if part]
    return ". ".join(parts)


def local_extract_clinical_context(
    *,
    encounter: dict[str, Any],
    conditions: list[dict[str, Any]],
    fhir_procedures: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    charge_items: list[dict[str, Any]],
) -> ClinicalContext:
    note = encounter_note_text(encounter)
    condition_text = " ".join(
        first_codeable_text(condition.get("code")) or "" for condition in conditions
    )
    procedure_text = " ".join(
        first_codeable_text(procedure.get("code")) or "" for procedure in fhir_procedures
    )
    searchable_text = " ".join(
        [
            note,
            condition_text,
            procedure_text,
            " ".join(str(item.get("description", "")) for item in charge_items),
            " ".join(str(item.get("name", "")) for item in attachments),
        ]
    ).lower()

    suspected_conditions: list[str] = []
    services: list[str] = []
    documentation_gaps: list[str] = []
    facts: list[str] = []

    if "osteoarthritis" in searchable_text and "knee" in searchable_text:
        suspected_conditions.append("knee osteoarthritis")
        facts.append("Encounter references knee osteoarthritis.")

    if "right knee" in searchable_text:
        facts.append("Laterality appears to be right knee.")

    if "total knee replacement" in searchable_text or "knee arthroplasty" in searchable_text:
        services.append("total knee replacement")
        facts.append("Encounter references total knee replacement or arthroplasty.")

    attached_names = " ".join(str(item.get("name", "")).lower() for item in attachments)
    if "radiology" in searchable_text and "radiology" not in attached_names:
        documentation_gaps.append("radiology_report")

    if not note:
        documentation_gaps.append("soap_note")

    return ClinicalContext(
        soap_note_excerpt=note[:700],
        chief_complaint=None,
        encounter_type=first_codeable_text(encounter.get("type")),
        suspected_conditions=suspected_conditions,
        documented_procedures=[],
        planned_or_performed_services=services,
        medical_necessity_summary=None,
        documentation_gaps=documentation_gaps,
        coding_relevant_facts=facts,
        confidence=0.55 if facts else 0.25,
        extraction_method="local_rule_based_fallback",
    )


# ---------------------------------------------------------------------------
# Draft claim construction
# ---------------------------------------------------------------------------


def payer_identifier(coverage: dict[str, Any]) -> str | None:
    payor = coverage.get("payor", [])
    if not payor:
        return None

    first_payor = payor[0]
    identifier = first_payor.get("identifier", {})
    return identifier.get("value") or first_payor.get("display") or first_payor.get("reference")


def payer_display(coverage: dict[str, Any]) -> str | None:
    payor = coverage.get("payor", [])
    if not payor:
        return None
    return payor[0].get("display") or payer_identifier(coverage)


def coverage_plan_name(coverage: dict[str, Any]) -> str | None:
    classes = coverage.get("class", [])
    if not classes:
        return None
    return classes[0].get("name") or classes[0].get("value")


def encounter_service_date(encounter: dict[str, Any]) -> str:
    period = encounter.get("period", {})
    start = period.get("start") or encounter.get("service_date")
    if not start:
        return datetime.now(UTC).date().isoformat()
    return str(start).split("T")[0]


def total_submitted_amount(charge_items: list[dict[str, Any]]) -> tuple[float, str]:
    total = 0.0
    currency = "AED"
    for item in charge_items:
        amount = item.get("amount")
        if amount is None:
            amount = item.get("unitPrice", {}).get("value")
        total += float(amount or 0.0)
        currency = item.get("currency") or item.get("unitPrice", {}).get("currency") or currency
    return total, currency


def claim_id_for(case_id: str, encounter: dict[str, Any]) -> str:
    encounter_id = encounter.get("id", "UNKNOWN")
    compact_case = case_id.replace("CASE-", "")
    compact_encounter = encounter_id.replace("ENC-", "")
    return f"CLM-{compact_case}-{compact_encounter}"


def procedure_system_uri(system: str | None) -> str:
    normalized = (system or "").upper()
    if normalized == "CDT":
        return "http://www.ada.org/cdt"
    if normalized == "HCPCS":
        return "https://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"
    if normalized == "CPT":
        return "http://www.ama-assn.org/go/cpt"
    return "urn:velo-claim:code-system:procedure"


def diagnosis_system_uri(system: str | None) -> str:
    normalized = (system or "").upper()
    if normalized == "ICD-10":
        return "http://hl7.org/fhir/sid/icd-10"
    if normalized == "SNOMED-CT":
        return "http://snomed.info/sct"
    return "urn:velo-claim:code-system:diagnosis"


def normalized_config_token(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "AUTO").upper()) or "AUTO"


def normalize_claim_format(value: str | None) -> str:
    return CLAIM_FORMAT_ALIASES.get(normalized_config_token(value), "AUTO")


def normalize_jurisdiction(value: str | None) -> str:
    return JURISDICTION_ALIASES.get(normalized_config_token(value), "AUTO")


def resource_has_identifier_system(resource: dict[str, Any], text: str) -> bool:
    needle = text.lower()
    return any(
        needle in str(identifier.get("system", "")).lower()
        for identifier in resource.get("identifier", [])
    )


def claim_financials(charge_items: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "gross": 0.0,
        "net": 0.0,
        "patient_share": 0.0,
        "billed": 0.0,
        "currency": "AED",
    }
    for item in charge_items:
        currency = money_currency(item, totals["currency"])
        gross = money_value(
            item,
            "gross",
            "grossAmount",
            "ClaimGross",
            "amount",
            "unitPrice",
        )
        net = money_value(
            item,
            "net",
            "netAmount",
            "ClaimNet",
            "amount",
            "unitPrice",
            default=gross,
        )
        patient_share = money_value(
            item,
            "patient_share",
            "patientShare",
            "PatientShare",
            "ClaimPatientShare",
            "copay",
            default=0.0,
        )
        totals["gross"] += gross
        totals["net"] += net
        totals["patient_share"] += patient_share
        totals["billed"] += net
        totals["currency"] = currency
    return totals


def money_value(item: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("value")
        if value not in (None, ""):
            return float(value)
    return float(default)


def money_currency(item: dict[str, Any], default: str = "AED") -> str:
    for key in ("currency", "unitPrice", "net", "gross", "amount"):
        value = item.get(key)
        if isinstance(value, dict) and value.get("currency"):
            return str(value["currency"])
        if key == "currency" and value:
            return str(value)
    return default


def quantity_value(item: dict[str, Any], default: int = 1) -> int:
    value = item.get("quantity", default)
    if isinstance(value, dict):
        value = value.get("value", default)
    return max(1, int(float(value or default)))


def decimal_text(value: Any) -> str:
    return f"{float(value or 0.0):.2f}"


def xml_text(parent: Element, tag: str, value: Any) -> Element:
    element = SubElement(parent, tag)
    element.text = "" if value is None else str(value)
    return element


def pretty_xml(root: Element) -> str:
    rough = tostring(root, encoding="utf-8")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


def format_for_jurisdiction(jurisdiction: str) -> str:
    normalized = normalize_jurisdiction(jurisdiction)
    if normalized == "KSA":
        return "NPHIES"
    if normalized == "ABU_DHABI":
        return "SHAFAFIYA"
    if normalized == "DUBAI":
        return "ECLAIMLINK"
    return "AUTO"


def infer_claim_format(
    *,
    requested_format: str | None,
    jurisdiction: str | None,
    patient: dict[str, Any],
    coverage: dict[str, Any],
    provider: dict[str, Any],
    facility: dict[str, Any],
    currency: str,
) -> str:
    explicit_format = normalize_claim_format(requested_format)
    if explicit_format != "AUTO":
        return explicit_format

    jurisdiction_format = format_for_jurisdiction(jurisdiction or "AUTO")
    if jurisdiction_format != "AUTO":
        return jurisdiction_format

    payer_text = " ".join(
        str(value or "")
        for value in [
            payer_identifier(coverage),
            payer_display(coverage),
            coverage_plan_name(coverage),
        ]
    ).lower()

    if (
        resource_has_identifier_system(facility, "dha")
        or resource_has_identifier_system(provider, "dha")
        or "dha" in payer_text
        or "dubai" in payer_text
    ):
        return "ECLAIMLINK"

    if (
        resource_has_identifier_system(facility, "doh")
        or resource_has_identifier_system(provider, "doh")
        or "daman" in payer_text
        or "thiqa" in payer_text
        or "abu dhabi" in payer_text
    ):
        return "SHAFAFIYA"

    if (
        resource_has_identifier_system(facility, "nphies")
        or resource_has_identifier_system(provider, "nphies")
        or resource_has_identifier_system(patient, "nin")
        or resource_has_identifier_system(patient, "iqama")
        or currency.upper() == "SAR"
        or "tawuniya" in payer_text
    ):
        return "NPHIES"

    return "CANONICAL"


def jurisdiction_for_format(format_name: str, explicit_jurisdiction: str | None) -> str:
    normalized_jurisdiction = normalize_jurisdiction(explicit_jurisdiction)
    if normalized_jurisdiction != "AUTO":
        return normalized_jurisdiction
    if format_name == "NPHIES":
        return "KSA"
    if format_name == "SHAFAFIYA":
        return "ABU_DHABI"
    if format_name == "ECLAIMLINK":
        return "DUBAI"
    return "INTERNAL"


def build_line_items(
    *,
    procedure_codes: list[dict[str, Any]],
    charge_items: list[dict[str, Any]],
    service_date: str,
) -> list[dict[str, Any]]:
    count = max(len(procedure_codes), len(charge_items))
    lines: list[dict[str, Any]] = []

    for index in range(count):
        procedure = procedure_codes[index] if index < len(procedure_codes) else {}
        charge = charge_items[index] if index < len(charge_items) else {}
        gross = money_value(
            charge,
            "gross",
            "grossAmount",
            "ClaimGross",
            "amount",
            "unitPrice",
        )
        net = money_value(
            charge,
            "net",
            "netAmount",
            "ClaimNet",
            "amount",
            "unitPrice",
            default=gross,
        )
        patient_share = money_value(
            charge,
            "patient_share",
            "patientShare",
            "PatientShare",
            "ClaimPatientShare",
            "copay",
            default=0.0,
        )
        code = procedure.get("code") or charge.get("code") or charge.get("serviceCode")
        system = procedure.get("system") or charge.get("system") or charge.get("codeSystem")
        description = (
            procedure.get("description")
            or charge.get("description")
            or charge.get("service")
            or ""
        )

        lines.append(
            {
                "sequence": index + 1,
                "id": charge.get("id") or f"ACT-{index + 1}",
                "start": charge.get("start") or charge.get("service_date") or service_date,
                "code": code,
                "system": str(system or "GENERIC").upper(),
                "description": description,
                "quantity": procedure.get("units") or quantity_value(charge),
                "gross": gross,
                "net": net,
                "patient_share": patient_share,
                "currency": money_currency(charge),
            }
        )

    return lines


def encounter_type_for_format(encounter: dict[str, Any], format_name: str) -> str:
    class_code = str(encounter.get("class", {}).get("code") or "AMB").upper()
    if format_name == "NPHIES":
        return class_code
    if format_name == "ECLAIMLINK":
        return "2" if class_code in {"IMP", "INPATIENT"} else "1"
    if format_name == "SHAFAFIYA":
        if class_code in {"IMP", "INPATIENT"}:
            return "3"
        if class_code in {"SS", "DAYCASE", "DAY_CASE"}:
            return "5"
        return "1"
    return class_code


def activity_type_for_line(line: dict[str, Any], format_name: str) -> str:
    system = str(line.get("system") or "").upper()
    if system in {"CPT", "HCPCS", "CDT", "DDC", "DHA"}:
        return system
    if "DRUG" in system:
        return "DDC" if format_name == "ECLAIMLINK" else "PHARMACY"
    return "GENERIC"


NPHIES_BASE_URL = "http://nphies.sa/fhir/ksa/nphies-fs"
NPHIES_PROFILE_BUNDLE = f"{NPHIES_BASE_URL}/StructureDefinition/bundle"
NPHIES_PROFILE_MESSAGE_HEADER = f"{NPHIES_BASE_URL}/StructureDefinition/message-header"
NPHIES_PROFILE_PROFESSIONAL_CLAIM = f"{NPHIES_BASE_URL}/StructureDefinition/professional-claim"
NPHIES_PROFILE_PATIENT = f"{NPHIES_BASE_URL}/StructureDefinition/patient"
NPHIES_PROFILE_COVERAGE = f"{NPHIES_BASE_URL}/StructureDefinition/coverage"
NPHIES_PROFILE_PRACTITIONER = f"{NPHIES_BASE_URL}/StructureDefinition/practitioner"
NPHIES_PROFILE_PROVIDER_ORGANIZATION = f"{NPHIES_BASE_URL}/StructureDefinition/provider-organization"
NPHIES_PROFILE_INSURER_ORGANIZATION = f"{NPHIES_BASE_URL}/StructureDefinition/insurer-organization"
NPHIES_PROFILE_ENCOUNTER_CLAIM_AMB = f"{NPHIES_BASE_URL}/StructureDefinition/encounter-claim-AMB"
NPHIES_EXTENSION_ENCOUNTER = f"{NPHIES_BASE_URL}/StructureDefinition/extension-encounter"
NPHIES_MESSAGE_EVENT_SYSTEM = "http://nphies.sa/terminology/CodeSystem/ksa-message-events"
NPHIES_PROVIDER_LICENSE_SYSTEM = "http://nphies.sa/license/provider-license"
NPHIES_PAYER_LICENSE_SYSTEM = "http://nphies.sa/license/payer-license"
NPHIES_PRACTITIONER_LICENSE_SYSTEM = "http://nphies.sa/license/practitioner-license"
NPHIES_DIAGNOSIS_TYPE_SYSTEM = "http://nphies.sa/terminology/CodeSystem/diagnosis-type"
NPHIES_PROCESS_MESSAGE_ENDPOINT = "https://nphies.sa/fhir/$process-message"
DEFAULT_NPHIES_SOURCE_ENDPOINT = "https://velodoc.ai/fhir"
CLAIM_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/claim-type"
PROCESS_PRIORITY_SYSTEM = "http://terminology.hl7.org/CodeSystem/processpriority"
SUBSCRIBER_RELATIONSHIP_SYSTEM = "http://terminology.hl7.org/CodeSystem/subscriber-relationship"


def nphies_meta(profile: str) -> dict[str, Any]:
    return {"profile": [profile]}


def nphies_identifier(system: str, value: Any) -> dict[str, Any]:
    return {
        "use": "official",
        "system": system,
        "value": str(value or "UNKNOWN"),
    }


def nphies_entry(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "fullUrl": f"{resource['resourceType']}/{resource['id']}",
        "resource": resource,
    }


def nphies_message_id(claim_id: str, suffix: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"velo-claim:nphies:{claim_id}:{suffix}"))


def nphies_org_id(prefix: str, value: Any) -> str:
    token = normalize_token(str(value or prefix), prefix)
    return f"{prefix}-{token}"[:64]


def nphies_patient_identifier(patient: dict[str, Any]) -> dict[str, Any]:
    patient_id = patient.get("emirates_id") or patient.get("member_id") or patient.get("id")
    identifier_type = "NI" if str(patient_id or "").startswith("1") else "PRC"
    return {
        "use": "official",
        "type": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                    "code": identifier_type,
                }
            ]
        },
        "system": "http://nphies.sa/identifier/patient",
        "value": str(patient_id or "UNKNOWN"),
    }


def human_name_from_source(
    display_name: str | None,
    source_names: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    source_name = source_names[0] if source_names else {}
    text = source_name.get("text") or display_name or ""
    given = source_name.get("given")
    family = source_name.get("family")

    if not given or not family:
        parts = [part for part in str(text).replace(".", " ").split() if part.lower() != "dr"]
        if not given and len(parts) > 1:
            given = parts[:-1]
        if not family and parts:
            family = parts[-1]

    name = {"text": text or str(family or "")}
    if family:
        name["family"] = str(family)
    if given:
        name["given"] = [str(item) for item in given] if isinstance(given, list) else [str(given)]
    if source_name.get("prefix"):
        prefix = source_name["prefix"]
        name["prefix"] = [str(item) for item in prefix] if isinstance(prefix, list) else [str(prefix)]
    return name


def build_nphies_practitioner(canonical_claim: dict[str, Any]) -> dict[str, Any]:
    provider = canonical_claim["provider"]
    source_provider = canonical_claim.get("source_resources", {}).get("provider", {})
    license_number = provider.get("license_number") or provider.get("id")
    return {
        "resourceType": "Practitioner",
        "id": provider.get("id") or nphies_org_id("PRAC", license_number),
        "meta": nphies_meta(NPHIES_PROFILE_PRACTITIONER),
        "identifier": [nphies_identifier(NPHIES_PRACTITIONER_LICENSE_SYSTEM, license_number)],
        "active": True,
        "name": [human_name_from_source(provider.get("name"), source_provider.get("name", []))],
    }


def build_nphies_provider_organization(canonical_claim: dict[str, Any]) -> dict[str, Any]:
    provider = canonical_claim["provider"]
    license_number = provider.get("facility_license_number") or provider.get("facility_id")
    return {
        "resourceType": "Organization",
        "id": nphies_org_id("PROV", license_number),
        "meta": nphies_meta(NPHIES_PROFILE_PROVIDER_ORGANIZATION),
        "identifier": [nphies_identifier(NPHIES_PROVIDER_LICENSE_SYSTEM, license_number)],
        "active": True,
        "type": [
            {
                "coding": [
                    {
                        "system": "http://nphies.sa/terminology/CodeSystem/organization-type",
                        "code": "prov",
                        "display": "Healthcare Provider",
                    }
                ]
            }
        ],
        "name": provider.get("facility_name") or provider.get("name") or "Provider Organization",
    }


def build_nphies_insurer_organization(canonical_claim: dict[str, Any]) -> dict[str, Any]:
    payer = canonical_claim["payer"]
    payer_license = payer.get("id") or payer.get("name")
    return {
        "resourceType": "Organization",
        "id": nphies_org_id("INS", payer_license),
        "meta": nphies_meta(NPHIES_PROFILE_INSURER_ORGANIZATION),
        "identifier": [nphies_identifier(NPHIES_PAYER_LICENSE_SYSTEM, payer_license)],
        "active": True,
        "type": [
            {
                "coding": [
                    {
                        "system": "http://nphies.sa/terminology/CodeSystem/organization-type",
                        "code": "ins",
                        "display": "Insurance Company",
                    }
                ]
            }
        ],
        "name": payer.get("name") or "Insurer Organization",
    }


def build_nphies_encounter(
    canonical_claim: dict[str, Any],
    provider_organization: dict[str, Any],
    practitioner_resource: dict[str, Any],
) -> dict[str, Any]:
    source_encounter = canonical_claim["source_resources"]["encounter"]
    period = source_encounter.get("period", {})
    start = period.get("start") or f"{canonical_claim['service_date']}T00:00:00+03:00"
    end = period.get("end") or (
        datetime.fromisoformat(str(start).replace("Z", "+00:00")) + timedelta(minutes=30)
    ).isoformat()
    class_code = str(source_encounter.get("class", {}).get("code") or "AMB").upper()
    return {
        "resourceType": "Encounter",
        "id": canonical_claim["encounter_id"],
        "meta": nphies_meta(NPHIES_PROFILE_ENCOUNTER_CLAIM_AMB),
        "identifier": [
            {
                "system": f"{NPHIES_BASE_URL}/identifier/encounter",
                "value": canonical_claim["encounter_id"],
            }
        ],
        "status": str(source_encounter.get("status") or "finished").lower(),
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": class_code,
            "display": source_encounter.get("class", {}).get("display") or class_code,
        },
        "subject": {"reference": f"Patient/{canonical_claim['patient'].get('id')}"},
        "period": {"start": start, "end": end},
        "participant": [
            {"individual": {"reference": f"Practitioner/{practitioner_resource['id']}"}}
        ],
        "serviceProvider": {"reference": f"Organization/{provider_organization['id']}"},
    }


def build_nphies_bundle(canonical_claim: dict[str, Any]) -> dict[str, Any]:
    claim_id = canonical_claim["claim_id"]
    created_at = canonical_claim["created_at"]
    claim_resource = json.loads(json.dumps(canonical_claim["fhir_claim"]))
    patient = canonical_claim["patient"]
    payer = canonical_claim["payer"]
    provider = canonical_claim["provider"]
    provider_organization = build_nphies_provider_organization(canonical_claim)
    insurer_organization = build_nphies_insurer_organization(canonical_claim)
    practitioner_resource = build_nphies_practitioner(canonical_claim)
    encounter_resource = build_nphies_encounter(
        canonical_claim,
        provider_organization,
        practitioner_resource,
    )

    bundle_id = str(uuid.uuid4())
    message_header_id = nphies_message_id(claim_id, "message-header")

    claim_resource["meta"] = nphies_meta(NPHIES_PROFILE_PROFESSIONAL_CLAIM)
    claim_resource["extension"] = [
        *claim_resource.get("extension", []),
        {
            "url": NPHIES_EXTENSION_ENCOUNTER,
            "valueReference": {"reference": f"Encounter/{encounter_resource['id']}"},
        },
    ]
    claim_resource["provider"] = {"reference": f"Organization/{provider_organization['id']}"}
    claim_resource["insurer"] = {"reference": f"Organization/{insurer_organization['id']}"}
    claim_resource["total"] = {
        "value": canonical_claim["amount"]["net"],
        "currency": canonical_claim["amount"]["currency"],
    }
    if canonical_claim.get("prior_auth", {}).get("ref"):
        claim_resource["preAuthRef"] = [canonical_claim["prior_auth"]["ref"]]

    patient_resource = {
        "resourceType": "Patient",
        "id": patient.get("id"),
        "meta": nphies_meta(NPHIES_PROFILE_PATIENT),
        "active": True,
        "identifier": [nphies_patient_identifier(patient)],
        "name": [
            human_name_from_source(
                patient.get("name"),
                canonical_claim.get("source_resources", {}).get("patient", {}).get("name", []),
            )
        ],
        "birthDate": patient.get("date_of_birth"),
        "gender": patient.get("gender"),
    }

    coverage_resource = {
        "resourceType": "Coverage",
        "id": payer.get("coverage_id"),
        "meta": nphies_meta(NPHIES_PROFILE_COVERAGE),
        "status": str(payer.get("coverage_status") or "active").lower(),
        "subscriberId": patient.get("member_id"),
        "subscriber": {"reference": f"Patient/{patient.get('id')}"},
        "beneficiary": {"reference": f"Patient/{patient.get('id')}"},
        "relationship": {
            "coding": [
                {
                    "system": SUBSCRIBER_RELATIONSHIP_SYSTEM,
                    "code": "self",
                    "display": "Self",
                }
            ]
        },
        "payor": [{"reference": f"Organization/{insurer_organization['id']}"}],
    }
    if payer.get("coverage_period"):
        coverage_resource["period"] = payer["coverage_period"]

    message_header = {
        "resourceType": "MessageHeader",
        "id": message_header_id,
        "meta": nphies_meta(NPHIES_PROFILE_MESSAGE_HEADER),
        "eventCoding": {
            "system": NPHIES_MESSAGE_EVENT_SYSTEM,
            "code": "claim-request",
            "display": "Claim Request",
        },
        "destination": [
            {
                "endpoint": NPHIES_PROCESS_MESSAGE_ENDPOINT,
                "receiver": {
                    "type": "Organization",
                    "identifier": nphies_identifier(
                        NPHIES_PAYER_LICENSE_SYSTEM,
                        payer.get("id"),
                    ),
                },
            }
        ],
        "sender": {
            "type": "Organization",
            "identifier": nphies_identifier(
                NPHIES_PROVIDER_LICENSE_SYSTEM,
                provider.get("facility_license_number"),
            ),
        },
        "source": {
            "endpoint": os.getenv("NPHIES_SOURCE_ENDPOINT") or DEFAULT_NPHIES_SOURCE_ENDPOINT,
            "name": provider.get("facility_name"),
        },
        "focus": [{"reference": f"Claim/{claim_id}"}],
    }

    return {
        "resourceType": "Bundle",
        "meta": nphies_meta(NPHIES_PROFILE_BUNDLE),
        "type": "message",
        "id": bundle_id,
        "timestamp": created_at,
        "entry": [
            nphies_entry(message_header),
            nphies_entry(provider_organization),
            nphies_entry(insurer_organization),
            nphies_entry(practitioner_resource),
            nphies_entry(patient_resource),
            nphies_entry(coverage_resource),
            nphies_entry(encounter_resource),
            nphies_entry(claim_resource),
        ],
    }


def build_shafafiya_xml(canonical_claim: dict[str, Any]) -> str:
    root = Element("Claim.Submission")
    header = SubElement(root, "Header")
    xml_text(header, "SenderID", canonical_claim["provider"].get("facility_license_number"))
    xml_text(header, "ReceiverID", canonical_claim["payer"].get("id"))
    xml_text(header, "TransactionDate", canonical_claim["created_at"])
    xml_text(header, "RecordCount", 1)
    xml_text(header, "DispositionFlag", "PRODUCTION_DRAFT")

    claim = SubElement(root, "Claim")
    patient = canonical_claim["patient"]
    provider = canonical_claim["provider"]
    payer = canonical_claim["payer"]
    amount = canonical_claim["amount"]
    prior_auth = canonical_claim["prior_auth"]

    xml_text(claim, "ID", canonical_claim["claim_id"])
    xml_text(claim, "MemberID", patient.get("member_id"))
    xml_text(claim, "EmiratesIDNumber", patient.get("emirates_id"))
    xml_text(claim, "MemberBirthDate", patient.get("date_of_birth"))
    xml_text(claim, "MemberGender", patient.get("gender"))
    xml_text(claim, "PayerID", payer.get("id"))
    xml_text(claim, "ProviderID", provider.get("facility_license_number"))
    xml_text(claim, "ClaimGross", decimal_text(amount.get("gross")))
    xml_text(claim, "ClaimNet", decimal_text(amount.get("net")))
    xml_text(claim, "ClaimPatientShare", decimal_text(amount.get("patient_share")))

    if prior_auth.get("ref"):
        resubmission = SubElement(claim, "Resubmission")
        xml_text(resubmission, "OriginalClaimID", prior_auth["ref"])

    encounter = SubElement(claim, "Encounter")
    xml_text(encounter, "FacilityID", provider.get("facility_license_number"))
    xml_text(encounter, "Type", canonical_claim["format_context"]["encounter_type"])
    xml_text(encounter, "Start", canonical_claim["service_date"])
    xml_text(encounter, "End", canonical_claim["service_date"])

    for diagnosis in canonical_claim.get("diagnoses", []):
        diagnosis_element = SubElement(encounter, "Diagnosis")
        xml_text(diagnosis_element, "Type", diagnosis.get("type", "principal"))
        xml_text(diagnosis_element, "Code", diagnosis.get("code"))
        xml_text(diagnosis_element, "Description", diagnosis.get("description"))

    for line in canonical_claim["line_items"]:
        activity = SubElement(encounter, "Activity")
        xml_text(activity, "ID", line["id"])
        xml_text(activity, "Start", line["start"])
        xml_text(activity, "Type", activity_type_for_line(line, "SHAFAFIYA"))
        xml_text(activity, "Code", line.get("code"))
        xml_text(activity, "CodeSystem", line.get("system"))
        xml_text(activity, "Quantity", line.get("quantity"))
        xml_text(activity, "Net", decimal_text(line.get("net")))
        xml_text(activity, "Clinician", provider.get("license_number"))
        if prior_auth.get("ref"):
            xml_text(activity, "PriorAuthorizationID", prior_auth["ref"])

    attachments = SubElement(claim, "Attachments")
    for attachment in canonical_claim.get("attachments", []):
        attachment_element = SubElement(attachments, "Attachment")
        xml_text(attachment_element, "FileName", attachment.get("name"))
        xml_text(attachment_element, "Type", attachment.get("type"))
        xml_text(attachment_element, "Status", attachment.get("status"))

    return pretty_xml(root)


def build_eclaimlink_xml(canonical_claim: dict[str, Any]) -> str:
    root = Element("Claim.Submission")
    header = SubElement(root, "Header")
    xml_text(header, "SenderID", canonical_claim["provider"].get("facility_license_number"))
    xml_text(header, "ReceiverID", canonical_claim["payer"].get("id"))
    xml_text(header, "TransactionDate", canonical_claim["created_at"])
    xml_text(header, "RecordCount", 1)

    claim = SubElement(root, "Claim")
    patient = canonical_claim["patient"]
    provider = canonical_claim["provider"]
    payer = canonical_claim["payer"]
    amount = canonical_claim["amount"]
    prior_auth = canonical_claim["prior_auth"]

    xml_text(claim, "ID", canonical_claim["claim_id"])
    xml_text(claim, "MemberID", patient.get("member_id"))
    xml_text(claim, "EmiratesIDNumber", patient.get("emirates_id"))
    xml_text(claim, "PayerID", payer.get("id"))
    xml_text(claim, "ProviderID", provider.get("facility_license_number"))
    xml_text(claim, "GrossAmount", decimal_text(amount.get("gross")))
    xml_text(claim, "PatientShare", decimal_text(amount.get("patient_share")))
    xml_text(claim, "NetAmount", decimal_text(amount.get("net")))
    if prior_auth.get("ref"):
        xml_text(claim, "AuthorizationID", prior_auth["ref"])

    encounter = SubElement(claim, "Encounter")
    xml_text(encounter, "FacilityID", provider.get("facility_license_number"))
    xml_text(encounter, "Type", canonical_claim["format_context"]["encounter_type"])
    xml_text(encounter, "Start", canonical_claim["service_date"])

    for diagnosis in canonical_claim.get("diagnoses", []):
        diagnosis_element = SubElement(encounter, "Diagnosis")
        xml_text(diagnosis_element, "DiagnosisCode", diagnosis.get("code"))
        xml_text(diagnosis_element, "Type", diagnosis.get("type", "principal"))

    for line in canonical_claim["line_items"]:
        activity = SubElement(encounter, "Activity")
        xml_text(activity, "ID", line["id"])
        xml_text(activity, "ActivityStart", line["start"])
        xml_text(activity, "Type", activity_type_for_line(line, "ECLAIMLINK"))
        xml_text(activity, "Code", line.get("code"))
        xml_text(activity, "CodeSystem", line.get("system"))
        xml_text(activity, "Quantity", line.get("quantity"))
        xml_text(activity, "Net", decimal_text(line.get("net")))
        xml_text(activity, "OrderingClinician", provider.get("license_number"))
        if prior_auth.get("ref"):
            xml_text(activity, "AuthorizationID", prior_auth["ref"])

    attachments = SubElement(claim, "Attachments")
    for attachment in canonical_claim.get("attachments", []):
        attachment_element = SubElement(attachments, "Attachment")
        xml_text(attachment_element, "FileName", attachment.get("name"))
        xml_text(attachment_element, "Type", attachment.get("type"))
        xml_text(attachment_element, "Status", attachment.get("status"))

    return pretty_xml(root)


def claim_format_warnings(canonical_claim: dict[str, Any], format_name: str) -> list[str]:
    warnings: list[str] = []
    patient = canonical_claim["patient"]
    provider = canonical_claim["provider"]
    source_patient = canonical_claim.get("source_resources", {}).get("patient", {})
    source_facility = canonical_claim.get("source_resources", {}).get("facility", {})

    if format_name == "NPHIES":
        if canonical_claim["amount"]["currency"].upper() != "SAR":
            warnings.append("NPHIES usually expects SAR; no currency conversion was applied.")
        if not (
            resource_has_identifier_system(source_patient, "nin")
            or resource_has_identifier_system(source_patient, "iqama")
        ):
            warnings.append("NPHIES patient NIN/IQAMA identifier is missing.")
        if not resource_has_identifier_system(source_facility, "nphies"):
            warnings.append("NPHIES provider/facility identifier is missing.")

    if format_name == "SHAFAFIYA":
        if canonical_claim["amount"]["currency"].upper() != "AED":
            warnings.append("Shafafiya usually expects AED; no currency conversion was applied.")
        for field in ("emirates_id", "date_of_birth", "gender", "member_id"):
            if not patient.get(field):
                warnings.append(f"Shafafiya required patient field is missing: {field}.")
        if not provider.get("facility_license_number"):
            warnings.append("Shafafiya ProviderID/facility DOH license is missing.")
        if not resource_has_identifier_system(source_facility, "doh"):
            warnings.append("Shafafiya DOH facility identifier is missing.")
        if canonical_claim["format_context"]["encounter_type"] == "3" and not canonical_claim.get("drg"):
            warnings.append("Shafafiya inpatient claims may require IR-DRG before submission.")

    if format_name == "ECLAIMLINK":
        if canonical_claim["amount"]["currency"].upper() != "AED":
            warnings.append("eClaimLink usually expects AED; no currency conversion was applied.")
        if not patient.get("emirates_id"):
            warnings.append("eClaimLink Emirates ID is missing.")
        if not patient.get("member_id"):
            warnings.append("eClaimLink DHA member card number/member ID is missing.")
        if not provider.get("facility_license_number"):
            warnings.append("eClaimLink ProviderID/facility DHA code is missing.")
        if not resource_has_identifier_system(source_facility, "dha"):
            warnings.append("eClaimLink DHA facility identifier is missing.")
        has_pharmacy_line = any(
            activity_type_for_line(line, "ECLAIMLINK") == "DDC"
            for line in canonical_claim.get("line_items", [])
        )
        if has_pharmacy_line and not any(
            str(line.get("system", "")).upper() == "DDC"
            for line in canonical_claim.get("line_items", [])
        ):
            warnings.append("eClaimLink pharmacy activities require Dubai Drug Code.")

    return warnings


def build_submission_payload(
    *,
    canonical_claim: dict[str, Any],
    requested_format: str | None,
    requested_jurisdiction: str | None,
) -> dict[str, Any]:
    format_name = infer_claim_format(
        requested_format=requested_format,
        jurisdiction=requested_jurisdiction,
        patient=canonical_claim["source_resources"]["patient"],
        coverage=canonical_claim["source_resources"]["coverage"],
        provider=canonical_claim["source_resources"]["provider"],
        facility=canonical_claim["source_resources"]["facility"],
        currency=canonical_claim["amount"]["currency"],
    )
    jurisdiction = jurisdiction_for_format(format_name, requested_jurisdiction)
    canonical_claim["format_context"] = {
        "format": format_name,
        "jurisdiction": jurisdiction,
        "encounter_type": encounter_type_for_format(
            canonical_claim["source_resources"]["encounter"],
            format_name,
        ),
    }

    if format_name == "NPHIES":
        payload = build_nphies_bundle(canonical_claim)
        payload_type = "application/fhir+json"
    elif format_name == "SHAFAFIYA":
        payload = build_shafafiya_xml(canonical_claim)
        payload_type = "application/xml"
    elif format_name == "ECLAIMLINK":
        payload = build_eclaimlink_xml(canonical_claim)
        payload_type = "application/xml"
    else:
        payload = json.loads(json.dumps(canonical_claim, ensure_ascii=True, default=str))
        payload_type = "application/json"

    return {
        "format": format_name,
        "jurisdiction": jurisdiction,
        "payload_type": payload_type,
        "payload": payload,
        "warnings": claim_format_warnings(canonical_claim, format_name),
        "schema_status": "nphies_sandbox_ready"
        if format_name == "NPHIES"
        else "draft_adapter_not_payer_certified",
    }


def build_fhir_claim(
    *,
    claim_id: str,
    patient: dict[str, Any],
    coverage: dict[str, Any],
    encounter: dict[str, Any],
    provider: dict[str, Any],
    diagnosis_codes: list[dict[str, Any]],
    procedure_codes: list[dict[str, Any]],
    charge_items: list[dict[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    diagnosis = [
        {
            "sequence": index + 1,
            "diagnosisCodeableConcept": {
                "coding": [
                    {
                        "system": diagnosis_system_uri(item.get("system")),
                        "code": item["code"],
                        "display": item["description"],
                    }
                ],
                "text": item["description"],
            },
            "type": [
                {
                    "coding": [
                        {
                            "system": NPHIES_DIAGNOSIS_TYPE_SYSTEM,
                            "code": str(item.get("type") or "principal").lower(),
                            "display": (
                                "Principal Diagnosis"
                                if str(item.get("type") or "principal").lower() == "principal"
                                else str(item.get("type") or "secondary").title()
                            ),
                        }
                    ]
                }
            ],
        }
        for index, item in enumerate(diagnosis_codes)
    ]

    procedures = [
        {
            "sequence": index + 1,
            "procedureCodeableConcept": {
                "coding": [
                    {
                        "system": procedure_system_uri(item.get("system")),
                        "code": item["code"],
                        "display": item["description"],
                    }
                ],
                "text": item["description"],
            },
            "date": encounter_service_date(encounter),
        }
        for index, item in enumerate(procedure_codes)
    ]

    claim_items = []
    for index, procedure in enumerate(procedure_codes):
        charge = charge_items[index] if index < len(charge_items) else {}
        amount = float(charge.get("amount", 0.0) or charge.get("unitPrice", {}).get("value", 0.0) or 0.0)
        currency = charge.get("currency") or charge.get("unitPrice", {}).get("currency") or "AED"
        claim_items.append(
            {
                "sequence": index + 1,
                "productOrService": {
                    "coding": [
                        {
                            "system": procedure_system_uri(procedure.get("system")),
                            "code": procedure["code"],
                            "display": procedure["description"],
                        }
                    ],
                    "text": procedure["description"],
                },
                "quantity": {"value": procedure.get("units", 1)},
                "unitPrice": {"value": amount, "currency": currency},
                "net": {"value": amount, "currency": currency},
            }
        )

    return {
        "resourceType": "Claim",
        "id": claim_id,
        "status": "active",
        "type": {
            "coding": [
                {
                    "system": CLAIM_TYPE_SYSTEM,
                    "code": "professional",
                    "display": "Professional",
                }
            ]
        },
        "use": "claim",
        "patient": {"reference": f"Patient/{patient.get('id')}"},
        "created": created_at.split("T")[0],
        "insurer": {"display": payer_display(coverage)},
        "provider": {"reference": f"Practitioner/{provider.get('id')}"},
        "priority": {
            "coding": [
                {
                    "system": PROCESS_PRIORITY_SYSTEM,
                    "code": "normal",
                    "display": "Normal",
                }
            ]
        },
        "diagnosis": diagnosis,
        "procedure": procedures,
        "insurance": [
            {
                "sequence": 1,
                "focal": True,
                "coverage": {"reference": f"Coverage/{coverage.get('id')}"},
            }
        ],
        "item": claim_items,
    }


def build_draft_claim(
    *,
    case_id: str,
    patient: dict[str, Any],
    coverage: dict[str, Any],
    encounter: dict[str, Any],
    provider: dict[str, Any],
    facility: dict[str, Any],
    attachments: list[dict[str, Any]],
    charge_items: list[dict[str, Any]],
    clinical_context: ClinicalContext,
    source_codes: SourceCodeExtractionResult,
    payer_rules: list[dict[str, Any]] | None = None,
    claim_format: str | None = None,
    jurisdiction: str | None = None,
    trigger_source: str | None = None,
    encounter_acceptance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = utc_now()
    claim_id = claim_id_for(case_id, encounter)
    financials = claim_financials(charge_items)
    diagnosis_codes = [item.model_dump() for item in source_codes.diagnosis_codes]
    procedure_codes = [item.model_dump() for item in source_codes.procedure_codes]
    code_links = [item.model_dump() for item in source_codes.code_links]
    line_items = build_line_items(
        procedure_codes=procedure_codes,
        charge_items=charge_items,
        service_date=encounter_service_date(encounter),
    )

    fhir_claim = build_fhir_claim(
        claim_id=claim_id,
        patient=patient,
        coverage=coverage,
        encounter=encounter,
        provider=provider,
        diagnosis_codes=diagnosis_codes,
        procedure_codes=procedure_codes,
        charge_items=charge_items,
        created_at=created_at,
    )

    canonical_claim = {
        "claim_id": claim_id,
        "case_id": case_id,
        "patient": {
            "id": patient.get("id"),
            "name": first_text(patient.get("name", [])),
            "member_id": coverage.get("subscriberId")
            or first_identifier(patient, "velo/member-id", "member-id"),
            "emirates_id": first_identifier(patient, "uae/emirates-id", "emirates-id"),
            "date_of_birth": patient.get("birthDate"),
            "gender": patient.get("gender"),
        },
        "provider": {
            "id": provider.get("id"),
            "name": first_text(provider.get("name", [])),
            "license_number": first_identifier(
                provider,
                "velo/clinician-license",
                "doh/clinician-license",
                "npi",
            ),
            "specialty": first_codeable_text(provider.get("qualification", [{}])[0].get("code"))
            if provider.get("qualification")
            else provider.get("specialty"),
            "facility_id": facility.get("id"),
            "facility_name": facility.get("name"),
            "facility_license_number": first_identifier(
                facility,
                "doh/facility-license",
                "dha/facility-code",
                "nphies/provider-id",
            ),
        },
        "payer": {
            "id": payer_identifier(coverage),
            "name": payer_display(coverage),
            "plan": coverage_plan_name(coverage),
            "coverage_id": coverage.get("id"),
            "coverage_status": str(coverage.get("status", "")).upper(),
            "coverage_period": coverage.get("period"),
        },
        "diagnoses": diagnosis_codes,
        "procedures": procedure_codes,
        "line_items": line_items,
        "code_links": code_links,
        "prior_auth": {
            "ref": None,
            "approved": False,
            "expires": None,
        },
        "attachments": attachments,
        "amount": financials,
        "status": "DRAFT",
        "encounter_id": encounter.get("id"),
        "trigger_source": normalize_trigger_source(trigger_source),
        "encounter_acceptance": encounter_acceptance or {},
        "service_date": encounter_service_date(encounter),
        "created_at": created_at,
        "payer_rules": payer_rules or [],
        "clinical_context": clinical_context.model_dump(),
        "coding": {
            "method": source_codes.coding_method,
            "source": "encounter_fhir",
            "confidence": source_codes.confidence,
            "warnings": source_codes.coding_warnings,
            "code_links": code_links,
            "requires_validation": True,
            "validation_agent": "ClaimValidationAgent",
        },
        "human_approval_required": False,
        "approval_reason": "Prepared from source data; validation is handled by ClaimValidationAgent.",
        "fhir_claim": fhir_claim,
        "source_resources": {
            "patient": patient,
            "coverage": coverage,
            "encounter": encounter,
            "provider": provider,
            "facility": facility,
        },
    }
    submission = build_submission_payload(
        canonical_claim=canonical_claim,
        requested_format=claim_format,
        requested_jurisdiction=jurisdiction,
    )
    return {
        **canonical_claim,
        "claim_format": submission["format"],
        "jurisdiction": submission["jurisdiction"],
        "submission": submission,
    }


# ---------------------------------------------------------------------------
# LangGraph node functions
# ---------------------------------------------------------------------------


def collect_missing_fields(state: ClaimPreparationState) -> list[str]:
    required_paths = {
        "patient": state.get("patient"),
        "coverage": state.get("coverage"),
        "encounter": state.get("encounter"),
        "provider": state.get("provider"),
        "facility": state.get("facility"),
        "soap_fields": state.get("soap_fields"),
        "icd_codes": state.get("icd_codes"),
        "procedure_codes": state.get("procedure_codes"),
        "source_codes": state.get("source_codes"),
        "claim_draft": state.get("claim_draft"),
        "claim_payload": state.get("claim_payload"),
    }
    return [name for name, value in required_paths.items() if not value]


def supervisor_agent(state: ClaimPreparationState) -> ClaimPreparationState:
    case_id = state.get("case_id", "CASE-001")
    return {
        **state,
        "case_id": case_id,
        "source": state.get("source", "Velo Clinic"),
        "trigger_source": normalize_trigger_source(
            state.get("trigger_source") or os.getenv("DEFAULT_TRIGGER_SOURCE") or DEFAULT_TRIGGER_SOURCE
        ),
        "encounter_acceptance": state.get("encounter_acceptance", {}),
        "encounter_package": state.get("encounter_package", {}),
        "status": "SUPERVISOR_ROUTED",
        "current_step": "prepare_claim",
        "next_agent": "ClaimPreparationAgent",
        "errors": state.get("errors", []),
        "warnings": state.get("warnings", []),
        "missing_fields": state.get("missing_fields", []),
        "payer_rules": state.get("payer_rules", []),
        "use_medgemma": state.get("use_medgemma", env_bool("USE_MEDGEMMA", True)),
        "require_medgemma": state.get(
            "require_medgemma",
            env_bool("REQUIRE_MEDGEMMA", False),
        ),
        "medgemma_base_url": state.get("medgemma_base_url")
        or os.getenv("MEDGEMMA_BASE_URL", ""),
        "medgemma_api_key": state.get("medgemma_api_key") or os.getenv("MEDGEMMA_API_KEY", ""),
        "medgemma_model": state.get("medgemma_model")
        or os.getenv("MEDGEMMA_MODEL")
        or DEFAULT_MEDGEMMA_MODEL,
        "medgemma_api_style": state.get("medgemma_api_style")
        or os.getenv("MEDGEMMA_API_STYLE")
        or DEFAULT_MEDGEMMA_API_STYLE,
        "medgemma_generate_path": state.get("medgemma_generate_path")
        or os.getenv("MEDGEMMA_GENERATE_PATH")
        or DEFAULT_MEDGEMMA_GENERATE_PATH,
        "medgemma_used": False,
        "fhir_base_url": state.get("fhir_base_url") or os.getenv("FHIR_BASE_URL", ""),
        "fhir_access_token": state.get("fhir_access_token") or os.getenv("FHIR_ACCESS_TOKEN", ""),
        "fhir_auth_type": state.get("fhir_auth_type")
        or os.getenv("FHIR_AUTH_TYPE")
        or DEFAULT_FHIR_AUTH_TYPE,
        "fhir_token_url": state.get("fhir_token_url") or os.getenv("FHIR_TOKEN_URL", ""),
        "fhir_client_id": state.get("fhir_client_id") or os.getenv("FHIR_CLIENT_ID", ""),
        "fhir_client_secret": state.get("fhir_client_secret") or os.getenv("FHIR_CLIENT_SECRET", ""),
        "fhir_client_auth_method": state.get("fhir_client_auth_method")
        or os.getenv("FHIR_CLIENT_AUTH_METHOD")
        or DEFAULT_FHIR_CLIENT_AUTH_METHOD,
        "fhir_scope": state.get("fhir_scope") or os.getenv("FHIR_SCOPE") or DEFAULT_FHIR_SCOPE,
        "fhir_audience": state.get("fhir_audience") or os.getenv("FHIR_AUDIENCE", ""),
        "fhir_private_key_path": state.get("fhir_private_key_path")
        or os.getenv("FHIR_PRIVATE_KEY_PATH", ""),
        "fhir_key_id": state.get("fhir_key_id") or os.getenv("FHIR_KEY_ID", ""),
        "fhir_jwks_url": state.get("fhir_jwks_url") or os.getenv("FHIR_JWKS_URL", ""),
        "claim_format": state.get("claim_format")
        or os.getenv("DEFAULT_CLAIM_FORMAT")
        or DEFAULT_CLAIM_FORMAT,
        "jurisdiction": state.get("jurisdiction")
        or os.getenv("DEFAULT_JURISDICTION")
        or DEFAULT_JURISDICTION,
    }


def prepare_claim(state: ClaimPreparationState) -> ClaimPreparationState:
    """
    Main LangGraph node from the architecture diagram.

    Internal steps:
        1. fhir_reader.run(state)
        2. soap_chain.invoke(state)
        3. code_chain.invoke(state) to copy source diagnosis/procedure codes
        4. claim_builder.run(state)
    """

    try:
        working_state = {
            **state,
            "status": "CLAIM_PREPARATION_STARTED",
            "current_step": "prepare_claim.fhir_reader",
        }

        working_state = fhir_reader.run(working_state)
        if working_state.get("errors"):
            return {
                **working_state,
                "missing_fields": collect_missing_fields(working_state),
            }

        working_state = {
            **working_state,
            "current_step": "prepare_claim.soap_extractor",
        }
        working_state = soap_chain.invoke(working_state)

        working_state = {
            **working_state,
            "current_step": "prepare_claim.source_code_extractor",
        }
        working_state = code_chain.invoke(working_state)

        working_state = {
            **working_state,
            "current_step": "prepare_claim.claim_builder",
        }
        working_state = claim_builder.run(working_state)

        missing_fields = collect_missing_fields(working_state)
        return {
            **working_state,
            "missing_fields": missing_fields,
            "current_step": "prepare_claim",
            "status": "PREPARE_CLAIM_COMPLETED"
            if not missing_fields
            else "CLAIM_PREPARATION_FAILED",
        }
    except (ValidationError, Exception) as exc:
        failed_state = {
            **state,
            "status": "CLAIM_PREPARATION_FAILED",
            "errors": add_error(state, "PREPARE_CLAIM_FAILED", str(exc)),
        }
        return {
            **failed_state,
            "missing_fields": collect_missing_fields(failed_state),
        }


def route_after_prepare_claim(state: ClaimPreparationState) -> str:
    if state.get("errors") or state.get("missing_fields"):
        return "error"
    return "success"


def validate_claim(state: ClaimPreparationState) -> ClaimPreparationState:
    return {
        **state,
        "status": "READY_FOR_VALIDATION",
        "current_step": "validate_claim",
        "next_agent": "ClaimValidationAgent",
    }


def error_handler(state: ClaimPreparationState) -> ClaimPreparationState:
    return {
        **state,
        "status": "CLAIM_PREPARATION_FAILED",
        "current_step": "error_handler",
        "next_agent": "HumanReview",
        "missing_fields": state.get("missing_fields") or collect_missing_fields(state),
    }


def start_claim_preparation(state: ClaimPreparationState) -> ClaimPreparationState:
    try:
        case_id = state.get("case_id", "CASE-001")
        return {
            **state,
            "case_id": case_id,
            "source": state.get("source", "Velo Clinic"),
            "trigger_source": normalize_trigger_source(
                state.get("trigger_source") or os.getenv("DEFAULT_TRIGGER_SOURCE") or DEFAULT_TRIGGER_SOURCE
            ),
            "encounter_acceptance": state.get("encounter_acceptance", {}),
            "encounter_package": state.get("encounter_package", {}),
            "status": "CLAIM_PREPARATION_STARTED",
            "use_medgemma": state.get("use_medgemma", env_bool("USE_MEDGEMMA", True)),
            "require_medgemma": state.get(
                "require_medgemma",
                env_bool("REQUIRE_MEDGEMMA", False),
            ),
            "medgemma_base_url": state.get("medgemma_base_url") or os.getenv("MEDGEMMA_BASE_URL", ""),
            "medgemma_api_key": state.get("medgemma_api_key") or os.getenv("MEDGEMMA_API_KEY", ""),
            "medgemma_model": state.get("medgemma_model") or os.getenv("MEDGEMMA_MODEL") or DEFAULT_MEDGEMMA_MODEL,
            "medgemma_api_style": state.get("medgemma_api_style")
            or os.getenv("MEDGEMMA_API_STYLE")
            or DEFAULT_MEDGEMMA_API_STYLE,
            "medgemma_generate_path": state.get("medgemma_generate_path")
            or os.getenv("MEDGEMMA_GENERATE_PATH")
            or DEFAULT_MEDGEMMA_GENERATE_PATH,
            "medgemma_used": False,
            "fhir_base_url": state.get("fhir_base_url") or os.getenv("FHIR_BASE_URL", ""),
            "fhir_access_token": state.get("fhir_access_token") or os.getenv("FHIR_ACCESS_TOKEN", ""),
            "fhir_auth_type": state.get("fhir_auth_type")
            or os.getenv("FHIR_AUTH_TYPE")
            or DEFAULT_FHIR_AUTH_TYPE,
            "fhir_token_url": state.get("fhir_token_url") or os.getenv("FHIR_TOKEN_URL", ""),
            "fhir_client_id": state.get("fhir_client_id") or os.getenv("FHIR_CLIENT_ID", ""),
            "fhir_client_secret": state.get("fhir_client_secret") or os.getenv("FHIR_CLIENT_SECRET", ""),
            "fhir_client_auth_method": state.get("fhir_client_auth_method")
            or os.getenv("FHIR_CLIENT_AUTH_METHOD")
            or DEFAULT_FHIR_CLIENT_AUTH_METHOD,
            "fhir_scope": state.get("fhir_scope") or os.getenv("FHIR_SCOPE") or DEFAULT_FHIR_SCOPE,
            "fhir_audience": state.get("fhir_audience") or os.getenv("FHIR_AUDIENCE", ""),
            "fhir_private_key_path": state.get("fhir_private_key_path")
            or os.getenv("FHIR_PRIVATE_KEY_PATH", ""),
            "fhir_key_id": state.get("fhir_key_id") or os.getenv("FHIR_KEY_ID", ""),
            "fhir_jwks_url": state.get("fhir_jwks_url") or os.getenv("FHIR_JWKS_URL", ""),
            "claim_format": state.get("claim_format")
            or os.getenv("DEFAULT_CLAIM_FORMAT")
            or DEFAULT_CLAIM_FORMAT,
            "jurisdiction": state.get("jurisdiction")
            or os.getenv("DEFAULT_JURISDICTION")
            or DEFAULT_JURISDICTION,
            "errors": state.get("errors", []),
            "warnings": state.get("warnings", []),
            "payer_rules": state.get("payer_rules", []),
        }
    except Exception as exc:
        return {
            **state,
            "status": "CLAIM_PREPARATION_FAILED",
            "errors": add_error(state, "START_FAILED", str(exc)),
        }


def extract_clinical_context_node(state: ClaimPreparationState) -> ClaimPreparationState:
    try:
        return soap_chain.invoke(state)
    except (ValidationError, Exception) as exc:
        return {
            **state,
            "status": "CLAIM_PREPARATION_FAILED",
            "errors": add_error(state, "CLINICAL_CONTEXT_EXTRACTION_FAILED", str(exc)),
        }


def extract_source_codes_node(state: ClaimPreparationState) -> ClaimPreparationState:
    try:
        return code_chain.invoke(state)
    except (ValidationError, Exception) as exc:
        return {
            **state,
            "status": "CLAIM_PREPARATION_FAILED",
            "errors": add_error(state, "SOURCE_CODE_EXTRACTION_FAILED", str(exc)),
        }


def build_draft_claim_node(state: ClaimPreparationState) -> ClaimPreparationState:
    try:
        return claim_builder.run(state)
    except Exception as exc:
        return {
            **state,
            "status": "CLAIM_PREPARATION_FAILED",
            "errors": add_error(state, "DRAFT_CLAIM_BUILD_FAILED", str(exc)),
        }


def complete_claim_preparation(state: ClaimPreparationState) -> ClaimPreparationState:
    if state.get("errors"):
        return {**state, "status": "CLAIM_PREPARATION_FAILED"}
    return {**state, "status": "CLAIM_PREPARATION_COMPLETED"}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_claim_preparation_graph():
    graph = StateGraph(ClaimPreparationState)

    graph.add_node("supervisor_agent", supervisor_agent)
    graph.add_node("prepare_claim", prepare_claim)
    graph.add_node("validate_claim", validate_claim)
    graph.add_node("error_handler", error_handler)

    graph.add_edge(START, "supervisor_agent")
    graph.add_edge("supervisor_agent", "prepare_claim")
    graph.add_conditional_edges(
        "prepare_claim",
        route_after_prepare_claim,
        {"success": "validate_claim", "error": "error_handler"},
    )
    graph.add_edge("validate_claim", END)
    graph.add_edge("error_handler", END)

    return graph.compile()


claim_preparation_agent = build_claim_preparation_graph()


def run_claim_preparation(initial_state: ClaimPreparationState) -> ClaimPreparationState:
    """
    Run the Claim Preparation Agent.

    Preferred production input is a trusted encounter plus FHIR references.
    If the encounter reaches this agent, it is treated as already accepted by
    Velo Doctor or RCM:
        {
            "trigger_source": "VELO_DOCTOR_APPROVED_ENCOUNTER" | "RCM_MANUAL_UPLOAD",
            "encounter_package": {
                "encounter": {
                    "subject": {"reference": "Patient/P001"},
                    "serviceProvider": {"reference": "Organization/FAC-001"},
                    "participant": [{"individual": {"reference": "Practitioner/D001"}}]
                }
            }
        }

    Patient, coverage, provider, facility, conditions, procedures,
    DocumentReference, and ChargeItem can then be loaded from FHIR.
    Local tests may inject those resources directly in encounter_package.
    """

    return claim_preparation_agent.invoke(initial_state)


def load_input_state_from_json(path: str | Path) -> ClaimPreparationState:
    input_path = Path(path)
    if not input_path.is_absolute():
        input_path = Path(__file__).resolve().parent / input_path

    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Claim preparation input JSON must contain one object.")
    return data


# ---------------------------------------------------------------------------
# Local development example with injected FHIR-like resources
# ---------------------------------------------------------------------------


def example_input_state() -> ClaimPreparationState:
    return load_input_state_from_json("sample_inputs/claim_preparation_pneumonia.json")




if __name__ == "__main__":
    input_state = (
        load_input_state_from_json(sys.argv[1])
        if len(sys.argv) > 1
        else example_input_state()
    )
    result = run_claim_preparation(input_state)

    print("\n=== Final Status ===")
    print(result["status"])

    print("\n=== MedGemma Used ===")
    print(result.get("medgemma_used"))

    print("\n=== Claim Submission Format ===")
    print(
        {
            "format": result.get("claim_format"),
            "jurisdiction": result.get("jurisdiction"),
            "payload_type": result.get("claim_payload_type"),
        }
    )

    print("\n=== Draft Claim ===")
    pprint(result.get("claim"))

    print("\n=== Warnings ===")
    pprint(result.get("warnings", []))

    print("\n=== Errors ===")
    pprint(result.get("errors", []))
