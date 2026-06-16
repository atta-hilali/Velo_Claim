"""
Prior Authorization Agent for Velo Claim.

This is the third agent in the Velo Claim chain. It receives a prepared and
validated draft claim when prior authorization is required, builds the market
specific prior authorization request package, optionally submits it, polls for
asynchronous results, and stores the returned preAuthRef on the claim.

Design:
    - LangGraph controls the workflow and state transitions.
    - The rule engine decides whether prior authorization is required.
    - NPHIES uses a draft FHIR R4 preauthorization Bundle.
    - Shafafiya/eClaimLink use draft XML adapters.
    - External submission is dry-run by default.
    - Real payer/gateway adapters must be certified before production use.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from pprint import pprint
from typing import Any, Callable, Literal, TypedDict
from uuid import uuid4
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from payer_registry import infer_standard_for_payer, payer_alias_tokens


PriorAuthRouting = Literal[
    "NOT_REQUIRED",
    "ALREADY_VALID",
    "NEEDS_DOCUMENTS",
    "SUBMISSION_READY",
    "PENDING_PAYER",
    "PENDED_NPHIES",
    "PARTIAL",
    "APPROVED",
    "DENIED",
    "MORE_INFO_REQUIRED",
    "FAILED",
]

PriorAuthStatus = Literal[
    "PRIOR_AUTH_STARTED",
    "PRIOR_AUTH_INPUT_NORMALIZED",
    "PRIOR_AUTH_REQUIREMENT_DETECTED",
    "PRIOR_AUTH_NOT_REQUIRED",
    "PRIOR_AUTH_ALREADY_VALID",
    "PRIOR_AUTH_NEEDS_DOCUMENTS",
    "PRIOR_AUTH_REQUEST_BUILT",
    "PRIOR_AUTH_SUBMISSION_READY",
    "PRIOR_AUTH_SUBMITTED",
    "PRIOR_AUTH_PENDING",
    "PRIOR_AUTH_PENDED_NPHIES",
    "PRIOR_AUTH_PARTIAL",
    "PRIOR_AUTH_APPROVED",
    "PRIOR_AUTH_DENIED",
    "PRIOR_AUTH_MORE_INFO_REQUIRED",
    "PRIOR_AUTH_COMPLETED",
    "PRIOR_AUTH_FAILED",
]

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "data" / "payer_rules" / "default_rules.json"
DEFAULT_PRIOR_AUTH_EXTRACTION_REGISTER_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "prior_auth_extraction"
    / "velo_claim_prior_auth_extraction_register.json"
)
DEFAULT_PRIOR_AUTH_THREAD_PREFIX = "prior-auth"
DEFAULT_PRIOR_AUTH_DRY_RUN = True
DEFAULT_PRIOR_AUTH_AUTO_SUBMIT = False
DEFAULT_PRIOR_AUTH_POLL_ENABLED = False
DEFAULT_PRIOR_AUTH_DRY_RUN_STATUS = "queued"

NPHIES_MESSAGE_EVENT_SYSTEM = "http://nphies.sa/terminology/CodeSystem/ksa-message-events"
FHIR_CLAIM_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/claim-type"
FHIR_PROCESS_PRIORITY_SYSTEM = "http://terminology.hl7.org/CodeSystem/processpriority"


class ClaimForPriorAuth(BaseModel):
    claim_id: str
    patient_id: str
    payer_id: str
    provider_id: str | None = None
    organization_id: str | None = None
    encounter_id: str | None = None
    coverage_id: str | None = None
    date_of_service: str
    specialty: str | None = None
    claim_format: str | None = None
    jurisdiction: str | None = None
    icd_codes: list[str] = Field(default_factory=list)
    procedure_codes: list[str] = Field(default_factory=list)
    line_items: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    prior_auth: dict[str, Any] | None = None
    amount: dict[str, Any] = Field(default_factory=dict)
    patient: dict[str, Any] = Field(default_factory=dict)
    payer: dict[str, Any] = Field(default_factory=dict)
    provider: dict[str, Any] = Field(default_factory=dict)
    source_resources: dict[str, Any] = Field(default_factory=dict)


class PayerRule(BaseModel):
    rule_id: str
    payer_id: str
    version: str = "1.0"
    status: Literal["DRAFT", "PENDING_REVIEW", "ACTIVE", "RETIRED"] = "ACTIVE"
    rule_type: str
    effective_from: str | None = None
    effective_to: str | None = None
    jurisdiction: str | None = None
    specialty: str | None = None
    condition: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["FAIL", "WARN", "INFO"] = "FAIL"
    message: str
    max_deduction_per_layer: float | None = None


class PriorAuthRequirement(BaseModel):
    rule_id: str
    payer_id: str
    procedure_code: str
    procedure_description: str | None = None
    jurisdiction: str | None = None
    required_documents: list[str] = Field(default_factory=list)
    message: str
    fix_template: str | None = None
    severity: Literal["FAIL", "WARN", "INFO"] = "FAIL"


class PriorAuthState(TypedDict, total=False):
    thread_id: str
    raw_claim: dict[str, Any]
    validation_result: dict[str, Any]
    validation_report: dict[str, Any]
    claim: dict[str, Any]
    claim_input: dict[str, Any]
    payer_rules: list[dict[str, Any]]
    applicable_rules: list[dict[str, Any]]
    requirements: list[dict[str, Any]]
    existing_prior_auth: dict[str, Any]
    prior_auth_valid: bool
    missing_documents: list[str]
    available_documents: list[str]
    request_format: str
    jurisdiction: str
    request_payload: Any
    request_payload_type: str
    prior_auth_request: dict[str, Any]
    submission_endpoint: str
    submission_response: dict[str, Any]
    poll_response: dict[str, Any]
    pre_auth_ref: str
    response_outcome: str
    adjudication_mode: str
    nphies_generated: bool
    alternate_follow_up_required: bool
    updated_claim: dict[str, Any]
    report: dict[str, Any]
    dry_run: bool
    auto_submit: bool
    poll_enabled: bool
    simulated_response_status: str
    simulated_final_response_status: str
    simulated_final_decision: str
    routing: PriorAuthRouting
    status: PriorAuthStatus
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    prior_auth_events: list[dict[str, Any]]
    messages: list[dict[str, Any]]


def load_env_file(path: str | Path = ".env") -> None:
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


load_env_file()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_token(value: Any) -> str:
    return str(value or "").strip().lower()


def compact_token(value: Any) -> str:
    return "".join(char for char in normalize_token(value) if char.isalnum())


def normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def reference_id(reference: str | None) -> str | None:
    if not reference:
        return None
    return str(reference).split("/")[-1]


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value).split("T")[0])
        except ValueError:
            return None


def date_in_period(service_date: str, period: dict[str, Any]) -> bool:
    current = parse_iso_date(service_date)
    if not current:
        return True
    start = parse_iso_date(period.get("start"))
    end = parse_iso_date(period.get("end"))
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def add_error(
    state: PriorAuthState,
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
    state: PriorAuthState,
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


def add_event(
    state: PriorAuthState,
    node: str,
    status: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        *state.get("prior_auth_events", []),
        {
            "node": node,
            "status": status,
            "message": message,
            "metadata": metadata or {},
            "timestamp": utc_now(),
        },
    ]


def with_event(
    state: PriorAuthState,
    node: str,
    status: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> PriorAuthState:
    return {
        **state,
        "prior_auth_events": add_event(state, node, status, message, metadata),
    }


def first_text(items: list[dict[str, Any]] | None) -> str | None:
    if not items:
        return None
    item = items[0]
    if item.get("text"):
        return item["text"]
    given = item.get("given", [])
    family = item.get("family")
    parts = [*given, family] if isinstance(given, list) else [given, family]
    return " ".join(str(part) for part in parts if part) or None


def payer_match_tokens(value: Any) -> set[str]:
    token = compact_token(value)
    tokens = {normalize_token(value), token}
    aliases = {
        "tawuniya": {"tawuniya", "tawuniyasa", "twn"},
        "tawuniyasa": {"tawuniya", "tawuniyasa", "twn"},
        "twn": {"tawuniya", "tawuniyasa", "twn"},
        "daman": {"daman", "damanae", "damanuae", "damanabudhabi"},
        "damanae": {"daman", "damanae", "damanuae", "damanabudhabi"},
        "damanuae": {"daman", "damanae", "damanuae", "damanabudhabi"},
        "default": {"default", "*"},
    }
    tokens.update(aliases.get(token, set()))
    tokens.update(payer_alias_tokens(value))
    return {item for item in tokens if item}


def payer_matches(rule_payer_id: Any, claim_payer_id: Any) -> bool:
    rule_token = normalize_token(rule_payer_id)
    if rule_token in {"*", "default"}:
        return True
    return bool(payer_match_tokens(rule_payer_id) & payer_match_tokens(claim_payer_id))


def load_payer_rules(path: Path = DEFAULT_RULES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rules", [])
    return [PayerRule.model_validate(item).model_dump() for item in data]


def load_prior_auth_extraction_register(
    path: Path = DEFAULT_PRIOR_AUTH_EXTRACTION_REGISTER_PATH,
) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def prior_auth_extraction_summary(register: dict[str, Any] | None = None) -> dict[str, Any]:
    register = register if register is not None else load_prior_auth_extraction_register()
    data = register.get("data", {}) if isinstance(register, dict) else {}
    compact_rules = register.get("prior_auth_rules_compact", []) if isinstance(register, dict) else []
    shafafiya_services = data.get("shafafiya_pa_services", {}).get("records", [])
    missing_data = register.get("missing_data") or data.get("missing_data", {}).get("records", [])
    implementation_steps = register.get("implementation_steps") or data.get("implementation_steps", {}).get("records", [])
    ready_rule_count = len(
        [
            rule
            for rule in compact_rules
            if "ready" in normalize_token(rule.get("implementation_status"))
        ]
    )
    return {
        "artifact": register.get("artifact") if isinstance(register, dict) else None,
        "source_workbook": register.get("source_workbook") if isinstance(register, dict) else None,
        "compact_prior_auth_rule_count": len(compact_rules),
        "ready_rule_count": ready_rule_count,
        "shafafiya_service_count": len(shafafiya_services),
        "missing_data_count": len(missing_data),
        "implementation_step_count": len(implementation_steps),
        "production_gap": next(
            (
                item.get("value")
                for item in data.get("readme", {}).get("records", [])
                if normalize_token(item.get("field")) == "main production gap"
            ),
            None,
        ),
    }


def claim_from_state(state: PriorAuthState) -> dict[str, Any]:
    if state.get("claim") and state["claim"].get("claim_id"):
        return state["claim"]
    raw = state.get("raw_claim") or {}
    if raw.get("claim"):
        return raw["claim"]
    if raw.get("claim_draft"):
        return raw["claim_draft"]
    if raw.get("full_result", {}).get("claim"):
        return raw["full_result"]["claim"]
    if raw.get("full_result", {}).get("claim_draft"):
        return raw["full_result"]["claim_draft"]
    validation_result = state.get("validation_result") or {}
    if validation_result.get("raw_claim"):
        return validation_result["raw_claim"]
    if validation_result.get("claim") and validation_result["claim"].get("claim_id"):
        return validation_result["claim"]
    return raw


def claim_input_from_canonical(claim: dict[str, Any]) -> ClaimForPriorAuth:
    if "date_of_service" in claim and "payer_id" in claim:
        return ClaimForPriorAuth.model_validate(claim)

    patient = claim.get("patient", {})
    payer = claim.get("payer", {})
    provider = claim.get("provider", {})
    amount = claim.get("amount", {})
    procedures = as_list(claim.get("procedures"))
    diagnoses = as_list(claim.get("diagnoses"))
    line_items = as_list(claim.get("line_items"))
    source_resources = claim.get("source_resources") or {}

    procedure_codes = [
        normalize_code(item.get("code"))
        for item in procedures
        if item.get("code")
    ]
    if not procedure_codes:
        procedure_codes = [
            normalize_code(item.get("code"))
            for item in line_items
            if item.get("code")
        ]

    return ClaimForPriorAuth(
        claim_id=claim.get("claim_id") or claim.get("id") or "UNKNOWN_CLAIM",
        patient_id=patient.get("id") or reference_id(claim.get("patient_id")) or "UNKNOWN_PATIENT",
        payer_id=payer.get("id") or payer.get("name") or claim.get("payer_id") or "UNKNOWN_PAYER",
        provider_id=provider.get("id") or claim.get("provider_id"),
        organization_id=provider.get("facility_id") or claim.get("organization_id"),
        encounter_id=claim.get("encounter_id"),
        coverage_id=payer.get("coverage_id") or claim.get("coverage_id"),
        date_of_service=claim.get("service_date") or claim.get("date_of_service") or utc_now().split("T")[0],
        specialty=provider.get("specialty") or claim.get("specialty"),
        claim_format=claim.get("claim_format") or claim.get("submission", {}).get("format"),
        jurisdiction=claim.get("jurisdiction") or claim.get("submission", {}).get("jurisdiction"),
        icd_codes=[normalize_code(item.get("code")) for item in diagnoses if item.get("code")],
        procedure_codes=procedure_codes,
        line_items=line_items,
        attachments=as_list(claim.get("attachments")),
        prior_auth=claim.get("prior_auth"),
        amount=amount,
        patient=patient,
        payer=payer,
        provider=provider,
        source_resources=source_resources,
    )


def rule_matches(rule: dict[str, Any], claim: ClaimForPriorAuth) -> bool:
    if rule.get("status") != "ACTIVE":
        return False
    if rule.get("rule_type") not in {"PRIOR_AUTH", "PRIOR_AUTHORIZATION"}:
        return False
    if not payer_matches(rule.get("payer_id"), claim.payer_id):
        return False
    specialty = rule.get("specialty")
    if specialty and claim.specialty and compact_token(specialty) != compact_token(claim.specialty):
        return False
    service_date = parse_iso_date(claim.date_of_service)
    if rule.get("effective_from") and service_date:
        if service_date < parse_iso_date(rule["effective_from"]):
            return False
    if rule.get("effective_to") and service_date:
        if service_date > parse_iso_date(rule["effective_to"]):
            return False
    return True


def condition_code_values(condition: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        condition.get("cpt"),
        condition.get("code"),
        *as_list(condition.get("cpt_codes")),
        *as_list(condition.get("procedure_codes")),
    ]
    if condition.get("field") == "procedure_codes":
        values.extend(as_list(condition.get("value")))
    for key in ("value", "cpt_range"):
        raw_value = condition.get(key)
        if isinstance(raw_value, list):
            values.extend(raw_value)
    return {normalize_code(value) for value in values if normalize_code(value)}


def code_matches_condition_code(code: str, condition: dict[str, Any]) -> bool:
    normalized_code = normalize_code(code)
    if not normalized_code:
        return False
    if normalized_code in condition_code_values(condition):
        return True
    prefixes = [
        normalize_code(prefix)
        for prefix in [
            *as_list(condition.get("cpt_range_prefix")),
            *as_list(condition.get("procedure_code_prefix")),
        ]
        if normalize_code(prefix)
    ]
    return any(normalized_code.startswith(prefix) for prefix in prefixes)


def line_quantity(line: dict[str, Any]) -> int:
    value = line.get("quantity") or line.get("units") or 1
    if isinstance(value, dict):
        value = value.get("value", 1)
    return max(1, int(float(value or 1)))


def matching_lines_for_code(claim: ClaimForPriorAuth, code: str) -> list[dict[str, Any]]:
    return [
        line
        for line in claim.line_items
        if normalize_code(line.get("code") or line.get("serviceCode")) == normalize_code(code)
    ]


def rule_requires_auth_for_code(rule: dict[str, Any], claim: ClaimForPriorAuth, code: str) -> bool:
    condition = rule.get("condition", {})
    if not code_matches_condition_code(code, condition):
        return False
    if condition.get("operator") == "requires_auth_if_over_threshold":
        threshold = int(condition.get("threshold_units") or rule.get("action", {}).get("threshold_units") or 0)
        if threshold <= 0:
            return True
        return any(line_quantity(line) > threshold for line in matching_lines_for_code(claim, code))
    return True


def auth_covers_code(auth: dict[str, Any], code: str, service_date: str) -> bool:
    if not auth:
        return False
    status = normalize_token(auth.get("status"))
    approved = auth.get("approved") is True or status in {"approved", "active"}
    if not approved:
        return False
    codes = {
        normalize_code(item)
        for item in as_list(auth.get("cpt_codes") or auth.get("procedure_codes") or auth.get("codes"))
    }
    if codes and normalize_code(code) not in codes:
        return False
    valid_from = auth.get("valid_from") or auth.get("from")
    valid_to = auth.get("valid_to") or auth.get("to") or auth.get("expires")
    if (valid_from or valid_to) and not date_in_period(service_date, {"start": valid_from, "end": valid_to}):
        return False
    return True


def document_tokens(claim: ClaimForPriorAuth) -> set[str]:
    tokens: set[str] = set()
    for attachment in claim.attachments:
        if not isinstance(attachment, dict):
            continue
        for key in ("type", "category", "name", "title"):
            value = attachment.get(key)
            if value:
                tokens.add(normalize_token(value))
                tokens.add(compact_token(value))
    for document in as_list(claim.source_resources.get("DocumentReference")):
        if not isinstance(document, dict):
            continue
        for key in ("type", "category", "description", "title"):
            value = document.get(key)
            if isinstance(value, dict):
                value = value.get("text")
            if value:
                tokens.add(normalize_token(value))
                tokens.add(compact_token(value))
    return {token for token in tokens if token}


def required_documents(requirements: list[dict[str, Any]]) -> list[str]:
    docs: list[str] = []
    for requirement in requirements:
        for document in requirement.get("required_documents", []):
            if document and document not in docs:
                docs.append(document)
    return docs


def claim_procedure_description(claim: ClaimForPriorAuth, code: str) -> str | None:
    for line in claim.line_items:
        if normalize_code(line.get("code") or line.get("serviceCode")) == normalize_code(code):
            return line.get("description") or line.get("service")
    return None


def infer_prior_auth_format(claim: ClaimForPriorAuth) -> tuple[str, str]:
    requested_format = normalize_code(claim.claim_format)
    jurisdiction = normalize_code(claim.jurisdiction)
    payer_text = " ".join([claim.payer_id, claim.payer.get("name") or ""]).lower()
    currency = str(claim.amount.get("currency") or "").upper()

    if requested_format in {"NPHIES", "SHAFAFIYA", "ECLAIMLINK"}:
        return requested_format, jurisdiction or jurisdiction_for_format(requested_format)
    payer_registry_format = infer_standard_for_payer(
        claim.payer_id,
        claim.payer.get("name"),
        jurisdiction=jurisdiction,
    )
    if payer_registry_format:
        return payer_registry_format, jurisdiction or jurisdiction_for_format(payer_registry_format)
    if jurisdiction in {"KSA", "SAUDI", "SAUDIARABIA"} or currency == "SAR" or "tawuniya" in payer_text:
        return "NPHIES", "KSA"
    if jurisdiction in {"ABU_DHABI", "ABUDHABI", "DOH", "AE_AUH"} or "daman" in payer_text:
        return "SHAFAFIYA", "ABU_DHABI"
    if jurisdiction in {"DUBAI", "DHA", "AE_DXB"}:
        return "ECLAIMLINK", "DUBAI"
    return "CANONICAL", jurisdiction or "INTERNAL"


def jurisdiction_for_format(format_name: str) -> str:
    if format_name == "NPHIES":
        return "KSA"
    if format_name == "SHAFAFIYA":
        return "ABU_DHABI"
    if format_name == "ECLAIMLINK":
        return "DUBAI"
    return "INTERNAL"


def xml_text(parent: Element, tag: str, value: Any) -> Element:
    element = SubElement(parent, tag)
    element.text = "" if value is None else str(value)
    return element


def pretty_xml(root: Element) -> str:
    rough = tostring(root, encoding="utf-8")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


def prior_auth_endpoint_for_format(format_name: str) -> str:
    if format_name == "NPHIES":
        return (
            os.getenv("NPHIES_PRIOR_AUTH_URL")
            or os.getenv("NPHIES_PROCESS_MESSAGE_URL")
            or ""
        )
    if format_name == "SHAFAFIYA":
        return os.getenv("SHAFAFIYA_PRIOR_AUTH_URL", "")
    if format_name == "ECLAIMLINK":
        return os.getenv("ECLAIMLINK_PRIOR_AUTH_URL", "")
    return os.getenv("PRIOR_AUTH_ENDPOINT_URL", "")


def build_canonical_prior_auth_request(
    *,
    claim: ClaimForPriorAuth,
    requirements: list[dict[str, Any]],
    request_format: str,
    jurisdiction: str,
) -> dict[str, Any]:
    return {
        "prior_auth_request_id": f"PAR-{claim.claim_id}-{uuid4().hex[:8]}",
        "claim_id": claim.claim_id,
        "status": "DRAFT",
        "format": request_format,
        "jurisdiction": jurisdiction,
        "patient": claim.patient,
        "payer": claim.payer,
        "provider": claim.provider,
        "coverage_id": claim.coverage_id,
        "encounter_id": claim.encounter_id,
        "service_date": claim.date_of_service,
        "diagnosis_codes": claim.icd_codes,
        "procedure_codes": claim.procedure_codes,
        "line_items": claim.line_items,
        "requirements": requirements,
        "attachments": claim.attachments,
        "amount": claim.amount,
        "created_at": utc_now(),
        "schema_status": "draft_adapter_not_payer_certified",
    }


def build_nphies_prior_auth_bundle(request: dict[str, Any], claim: ClaimForPriorAuth) -> dict[str, Any]:
    prior_auth_id = request["prior_auth_request_id"]
    patient = claim.patient
    payer = claim.payer
    provider = claim.provider
    source_resources = claim.source_resources
    fhir_claim = {
        "resourceType": "Claim",
        "id": prior_auth_id,
        "status": "active",
        "meta": {
            "profile": ["http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/claim"]
        },
        "type": {
            "coding": [
                {
                    "system": FHIR_CLAIM_TYPE_SYSTEM,
                    "code": "professional",
                    "display": "Professional",
                }
            ]
        },
        "use": "preauthorization",
        "patient": {"reference": f"Patient/{claim.patient_id}"},
        "created": utc_now().split("T")[0],
        "insurer": {"display": payer.get("name") or claim.payer_id},
        "provider": {"reference": f"Practitioner/{claim.provider_id}"} if claim.provider_id else {"display": provider.get("name")},
        "priority": {
            "coding": [
                {
                    "system": FHIR_PROCESS_PRIORITY_SYSTEM,
                    "code": "normal",
                    "display": "Normal",
                }
            ]
        },
        "insurance": [
            {
                "sequence": 1,
                "focal": True,
                "coverage": {"reference": f"Coverage/{claim.coverage_id}"},
            }
        ],
        "diagnosis": [
            {
                "sequence": index + 1,
                "diagnosisCodeableConcept": {
                    "coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": code}]
                },
            }
            for index, code in enumerate(claim.icd_codes)
        ],
        "item": [
            {
                "sequence": index + 1,
                "productOrService": {
                    "coding": [{"code": code, "display": claim_procedure_description(claim, code)}]
                },
                "servicedDate": claim.date_of_service,
            }
            for index, code in enumerate(claim.procedure_codes)
        ],
    }

    return {
        "resourceType": "Bundle",
        "id": f"BND-{prior_auth_id}-{uuid4().hex}",
        "type": "message",
        "timestamp": utc_now(),
        "meta": {
            "profile": ["http://nphies.sa/fhir/ksa/nphies-fs/StructureDefinition/bundle"]
        },
        "entry": [
            {
                "fullUrl": f"urn:uuid:messageheader-{prior_auth_id}",
                "resource": {
                    "resourceType": "MessageHeader",
                    "id": f"MSG-{prior_auth_id}",
                    "eventCoding": {
                        "system": NPHIES_MESSAGE_EVENT_SYSTEM,
                        "code": "priorauth-request",
                    },
                    "source": {"endpoint": os.getenv("NPHIES_SOURCE_ENDPOINT", "https://velodoc.ai/fhir")},
                    "destination": [{"name": payer.get("name") or claim.payer_id}],
                    "focus": [{"reference": f"Claim/{prior_auth_id}"}],
                },
            },
            {
                "fullUrl": f"urn:uuid:patient-{claim.patient_id}",
                "resource": {
                    "resourceType": "Patient",
                    "id": claim.patient_id,
                    "name": source_resources.get("patient", {}).get("name") or [{"text": patient.get("name")}],
                    "gender": patient.get("gender"),
                    "birthDate": patient.get("date_of_birth"),
                },
            },
            {
                "fullUrl": f"urn:uuid:coverage-{claim.coverage_id}",
                "resource": source_resources.get("coverage")
                or {
                    "resourceType": "Coverage",
                    "id": claim.coverage_id,
                    "status": "active",
                    "subscriberId": patient.get("member_id"),
                    "payor": [{"display": payer.get("name") or claim.payer_id}],
                },
            },
            {
                "fullUrl": f"urn:uuid:provider-{claim.provider_id or 'provider'}",
                "resource": source_resources.get("provider")
                or {
                    "resourceType": "Practitioner",
                    "id": claim.provider_id,
                    "name": [{"text": provider.get("name")}],
                },
            },
            {
                "fullUrl": f"urn:uuid:claim-{prior_auth_id}",
                "resource": fhir_claim,
            },
        ],
    }


def build_shafafiya_prior_auth_xml(request: dict[str, Any], claim: ClaimForPriorAuth) -> str:
    root = Element("Prior.Authorization.Request")
    header = SubElement(root, "Header")
    xml_text(header, "SenderID", claim.provider.get("facility_license_number"))
    xml_text(header, "ReceiverID", claim.payer_id)
    xml_text(header, "TransactionDate", request["created_at"])
    xml_text(header, "RecordCount", 1)

    authorization = SubElement(root, "Authorization")
    xml_text(authorization, "ID", request["prior_auth_request_id"])
    xml_text(authorization, "ClaimID", claim.claim_id)
    xml_text(authorization, "MemberID", claim.patient.get("member_id"))
    xml_text(authorization, "EmiratesIDNumber", claim.patient.get("emirates_id"))
    xml_text(authorization, "PayerID", claim.payer_id)
    xml_text(authorization, "ProviderID", claim.provider.get("facility_license_number"))
    xml_text(authorization, "EncounterID", claim.encounter_id)
    xml_text(authorization, "ServiceDate", claim.date_of_service)

    diagnoses = SubElement(authorization, "Diagnoses")
    for code in claim.icd_codes:
        diagnosis = SubElement(diagnoses, "Diagnosis")
        xml_text(diagnosis, "Code", code)

    activities = SubElement(authorization, "Activities")
    for index, code in enumerate(claim.procedure_codes):
        activity = SubElement(activities, "Activity")
        xml_text(activity, "ID", f"ACT-{index + 1}")
        xml_text(activity, "Code", code)
        xml_text(activity, "Start", claim.date_of_service)
        xml_text(activity, "Quantity", 1)

    attachments = SubElement(authorization, "Attachments")
    for attachment in claim.attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_element = SubElement(attachments, "Attachment")
        xml_text(attachment_element, "FileName", attachment.get("name"))
        xml_text(attachment_element, "Type", attachment.get("type"))

    return pretty_xml(root)


def build_prior_auth_payload(
    *,
    request: dict[str, Any],
    claim: ClaimForPriorAuth,
    request_format: str,
) -> tuple[Any, str]:
    if request_format == "NPHIES":
        return build_nphies_prior_auth_bundle(request, claim), "application/fhir+json"
    if request_format in {"SHAFAFIYA", "ECLAIMLINK"}:
        return build_shafafiya_prior_auth_xml(request, claim), "application/xml"
    return request, "application/json"


def nested_values(value: Any, key: str) -> list[Any]:
    matches: list[Any] = []
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                matches.append(current_value)
            matches.extend(nested_values(current_value, key))
    elif isinstance(value, list):
        for item in value:
            matches.extend(nested_values(item, key))
    return matches


def first_nested_value(value: Any, *keys: str) -> Any:
    for key in keys:
        for match in nested_values(value, key):
            if match not in (None, ""):
                return match
    return None


def response_body(response: dict[str, Any]) -> Any:
    return response.get("body", response)


def response_has_meta_tag(value: Any, tag_code: str) -> bool:
    if not isinstance(value, dict):
        return False
    meta = value.get("meta", {})
    for tag in as_list(meta.get("tag")):
        if isinstance(tag, dict) and compact_token(tag.get("code") or tag.get("display")) == compact_token(tag_code):
            return True
    for entry in as_list(value.get("entry")):
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if isinstance(resource, dict) and response_has_meta_tag(resource, tag_code):
            return True
    return False


def is_nphies_generated_response(response: dict[str, Any]) -> bool:
    return bool(
        response.get("nphies_generated")
        or response_has_meta_tag(response_body(response), "nphies-generated")
    )


def extract_response_outcome(response: dict[str, Any]) -> str | None:
    body = response_body(response)
    raw_outcome = (
        response.get("outcome")
        or response.get("status")
        or first_nested_value(body, "outcome", "authorizationOutcome")
    )
    return str(raw_outcome) if raw_outcome not in (None, "") else None


def extract_final_decision(response: dict[str, Any]) -> str | None:
    body = response_body(response)
    raw_decision = (
        response.get("decision")
        or response.get("authorization_status")
        or response.get("authorizationStatus")
        or first_nested_value(
            body,
            "decision",
            "authorizationStatus",
            "authorization_status",
            "preAuthStatus",
            "statusReason",
            "disposition",
        )
    )
    if raw_decision not in (None, ""):
        return str(raw_decision)
    return None


def normalize_decision(value: Any) -> str:
    decision = normalize_token(value)
    compact = compact_token(value)
    if compact in {"approved", "approve", "authorizationapproved", "preauthapproved"}:
        return "approved"
    if compact in {"denied", "deny", "rejected", "declined", "authorizationdenied"}:
        return "denied"
    if compact in {"moreinforequired", "additionaldocumentation", "requestadditionaldocumentation", "pendedforinformation"}:
        return "more_info_required"
    if decision in {"approved", "active"}:
        return "approved"
    if decision in {"denied", "rejected", "declined"}:
        return "denied"
    if decision in {"more_info_required", "more-info-required", "pended"}:
        return "more_info_required"
    return decision


def adjudication_mode_for_status(status: str) -> str:
    if status in {"approved", "denied", "more_info_required", "failed", "prepared"}:
        return "real_time"
    if status in {"queued", "partial", "pended", "complete"}:
        return "non_real_time"
    return "unknown"


def submit_payload(
    *,
    endpoint: str,
    payload: Any,
    payload_type: str,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=True)
    headers = {"Accept": "application/json", "Content-Type": payload_type}
    token = os.getenv("PRIOR_AUTH_ACCESS_TOKEN") or os.getenv("FHIR_ACCESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        endpoint,
        data=data.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                parsed_body: Any = json.loads(body)
            except json.JSONDecodeError:
                parsed_body = body
            response_payload = {
                "http_status": response.status,
                "body": parsed_body,
            }
            outcome = extract_response_outcome(response_payload)
            normalized_status = normalize_token(outcome) if outcome else ""
            fallback_status = "queued" if response.status in {202, 204} else "submitted"
            return {
                **response_payload,
                "status": normalized_status or fallback_status,
                "nphies_generated": response_has_meta_tag(parsed_body, "nphies-generated"),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": "failed",
            "http_status": exc.code,
            "body": body,
        }


def extract_pre_auth_ref(response: dict[str, Any]) -> str | None:
    for key in ("pre_auth_ref", "preAuthRef", "authorization_id", "authorizationId", "reference"):
        value = response.get(key)
        if value:
            return str(value)
    body = response.get("body")
    if isinstance(body, dict):
        value = first_nested_value(
            body,
            "preAuthRef",
            "pre_auth_ref",
            "authorizationId",
            "authorization_id",
            "priorAuthorizationId",
            "id",
        )
        if value:
            return str(value)
    return None


def normalize_response_status(response: dict[str, Any]) -> str:
    status = normalize_token(extract_response_outcome(response))
    compact_status = compact_token(status)

    if compact_status in {"complete", "completed"}:
        decision = normalize_decision(extract_final_decision(response))
        return decision if decision in {"approved", "denied", "more_info_required"} else "complete"

    if compact_status in {"approved", "active"}:
        return "approved"
    if compact_status in {"queued", "pending", "submitted", "inprogress"}:
        return "queued"
    if compact_status in {"partial", "partiallyadjudicated"}:
        return "partial"
    if compact_status in {"pended", "nphiespended", "pendeddelivery"}:
        return "pended"
    if compact_status in {"denied", "rejected", "declined"}:
        return "denied"
    if compact_status in {"moreinforequired", "additionaldocumentation", "pendedforinformation"}:
        return "more_info_required"
    if compact_status in {"prepared", "notsubmitted"}:
        return "prepared"
    if compact_status in {"failed", "error"}:
        return "failed"
    return status or "unknown"


def dry_run_nphies_response(
    *,
    outcome: str,
    endpoint: str,
    submitted: bool,
    decision: str | None = None,
) -> dict[str, Any]:
    normalized_outcome = normalize_token(outcome)
    normalized_decision = normalize_decision(decision)
    pre_auth_ref = (
        f"PA-{uuid4().hex[:12].upper()}"
        if normalized_outcome in {"approved"}
        or (normalized_outcome in {"complete", "completed"} and normalized_decision == "approved")
        else None
    )
    claim_response: dict[str, Any] = {
        "resourceType": "ClaimResponse",
        "id": f"CR-{uuid4().hex[:8]}",
        "outcome": normalized_outcome,
    }
    if decision:
        claim_response["decision"] = normalized_decision or str(decision)
        claim_response["disposition"] = str(decision)
    if pre_auth_ref:
        claim_response["preAuthRef"] = pre_auth_ref

    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "message",
        "entry": [{"resource": claim_response}],
    }
    if normalized_outcome in {"pended", "error", "failed"}:
        bundle["meta"] = {
            "tag": [
                {
                    "system": "http://nphies.sa/terminology/CodeSystem/meta-tags",
                    "code": "nphies-generated",
                    "display": "Generated by NPHIES",
                }
            ]
        }

    return {
        "status": normalized_outcome,
        "outcome": normalized_outcome,
        "submitted": submitted,
        "dry_run": True,
        "endpoint": endpoint or "dry-run",
        "pre_auth_ref": pre_auth_ref,
        "decision": normalized_decision or None,
        "body": bundle,
        "nphies_generated": response_has_meta_tag(bundle, "nphies-generated"),
    }


def poll_payload(
    *,
    response: dict[str, Any],
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    poll_url = os.getenv("PRIOR_AUTH_POLL_URL", "")
    reference = extract_pre_auth_ref(response) or response.get("poll_reference") or response.get("request_id")
    if not poll_url:
        return {
            **response,
            "polled": False,
            "message": "Polling is enabled but PRIOR_AUTH_POLL_URL is not configured.",
        }

    if reference and "{reference}" in poll_url:
        poll_url = poll_url.replace("{reference}", urllib.parse.quote(str(reference), safe=""))

    headers = {"Accept": "application/fhir+json, application/json"}
    token = os.getenv("PRIOR_AUTH_ACCESS_TOKEN") or os.getenv("FHIR_ACCESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(poll_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as http_response:
            body = http_response.read().decode("utf-8", errors="replace")
            try:
                parsed_body: Any = json.loads(body)
            except json.JSONDecodeError:
                parsed_body = body
            response_payload = {
                "http_status": http_response.status,
                "body": parsed_body,
                "polled": True,
            }
            outcome = extract_response_outcome(response_payload)
            return {
                **response,
                **response_payload,
                "status": normalize_token(outcome) if outcome else response.get("status", "queued"),
                "nphies_generated": response_has_meta_tag(parsed_body, "nphies-generated"),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            **response,
            "status": "failed",
            "http_status": exc.code,
            "body": body,
            "polled": True,
        }


def normalize_input_node(state: PriorAuthState) -> PriorAuthState:
    raw_claim = claim_from_state(state)
    claim_input = claim_input_from_canonical(raw_claim)
    return {
        **state,
        "claim": raw_claim,
        "claim_input": claim_input.model_dump(),
        "errors": state.get("errors", []),
        "warnings": state.get("warnings", []),
        "prior_auth_events": state.get("prior_auth_events", []),
        "messages": state.get("messages", []),
        "dry_run": state.get("dry_run", env_bool("PRIOR_AUTH_DRY_RUN", DEFAULT_PRIOR_AUTH_DRY_RUN)),
        "auto_submit": state.get(
            "auto_submit",
            env_bool("PRIOR_AUTH_AUTO_SUBMIT", DEFAULT_PRIOR_AUTH_AUTO_SUBMIT),
        ),
        "poll_enabled": state.get(
            "poll_enabled",
            env_bool("PRIOR_AUTH_POLL_ENABLED", DEFAULT_PRIOR_AUTH_POLL_ENABLED),
        ),
        "simulated_response_status": state.get("simulated_response_status")
        or os.getenv("PRIOR_AUTH_DRY_RUN_STATUS")
        or DEFAULT_PRIOR_AUTH_DRY_RUN_STATUS,
        "simulated_final_response_status": state.get("simulated_final_response_status")
        or os.getenv("PRIOR_AUTH_DRY_RUN_FINAL_STATUS")
        or "",
        "simulated_final_decision": state.get("simulated_final_decision")
        or os.getenv("PRIOR_AUTH_DRY_RUN_FINAL_DECISION")
        or "",
        "status": "PRIOR_AUTH_INPUT_NORMALIZED",
    }


def detect_prior_auth_requirement_node(state: PriorAuthState) -> PriorAuthState:
    claim = ClaimForPriorAuth.model_validate(state["claim_input"])
    rules = state.get("payer_rules") or load_payer_rules()
    applicable_rules = [rule for rule in rules if rule_matches(rule, claim)]
    requirements: list[dict[str, Any]] = []

    for rule in applicable_rules:
        for code in claim.procedure_codes:
            if not rule_requires_auth_for_code(rule, claim, code):
                continue
            action = rule.get("action", {})
            requirement = PriorAuthRequirement(
                rule_id=rule["rule_id"],
                payer_id=rule["payer_id"],
                procedure_code=code,
                procedure_description=claim_procedure_description(claim, code),
                jurisdiction=rule.get("jurisdiction"),
                required_documents=[
                    str(item)
                    for item in as_list(action.get("required_documents"))
                    if item
                ],
                message=rule.get("message") or f"Procedure code {code} requires prior authorization.",
                fix_template=action.get("fix_template"),
                severity=rule.get("severity", "FAIL"),
            )
            requirements.append(requirement.model_dump())

    return {
        **state,
        "payer_rules": rules,
        "applicable_rules": applicable_rules,
        "requirements": requirements,
        "status": "PRIOR_AUTH_REQUIREMENT_DETECTED",
    }


def complete_not_required_node(state: PriorAuthState) -> PriorAuthState:
    report = {
        "claim_id": state.get("claim_input", {}).get("claim_id"),
        "routing": "NOT_REQUIRED",
        "status": "PRIOR_AUTH_NOT_REQUIRED",
        "requirements": [],
        "message": "No prior authorization requirement was detected for this claim.",
        "generated_at": utc_now(),
    }
    return {
        **state,
        "routing": "NOT_REQUIRED",
        "report": report,
        "status": "PRIOR_AUTH_NOT_REQUIRED",
    }


def validate_existing_prior_auth_node(state: PriorAuthState) -> PriorAuthState:
    claim = ClaimForPriorAuth.model_validate(state["claim_input"])
    auth_records = as_list(state.get("existing_prior_auth"))
    if claim.prior_auth:
        auth_records.append(claim.prior_auth)

    required_codes = [item["procedure_code"] for item in state.get("requirements", [])]
    valid_auth = None
    for auth in auth_records:
        if not isinstance(auth, dict):
            continue
        if all(auth_covers_code(auth, code, claim.date_of_service) for code in required_codes):
            valid_auth = auth
            break

    return {
        **state,
        "existing_prior_auth": valid_auth or {},
        "prior_auth_valid": bool(valid_auth),
        "status": "PRIOR_AUTH_ALREADY_VALID" if valid_auth else state.get("status", "PRIOR_AUTH_REQUIREMENT_DETECTED"),
    }


def collect_documents_node(state: PriorAuthState) -> PriorAuthState:
    claim = ClaimForPriorAuth.model_validate(state["claim_input"])
    available = sorted(document_tokens(claim))
    required = required_documents(state.get("requirements", []))
    missing = [
        document
        for document in required
        if normalize_token(document) not in available and compact_token(document) not in available
    ]
    return {
        **state,
        "available_documents": available,
        "missing_documents": missing,
        "status": "PRIOR_AUTH_NEEDS_DOCUMENTS" if missing else state.get("status", "PRIOR_AUTH_REQUIREMENT_DETECTED"),
    }


def complete_needs_documents_node(state: PriorAuthState) -> PriorAuthState:
    report = {
        "claim_id": state.get("claim_input", {}).get("claim_id"),
        "routing": "NEEDS_DOCUMENTS",
        "status": "PRIOR_AUTH_NEEDS_DOCUMENTS",
        "requirements": state.get("requirements", []),
        "missing_documents": state.get("missing_documents", []),
        "message": "Prior authorization is required, but supporting documents are missing.",
        "generated_at": utc_now(),
    }
    return {
        **state,
        "routing": "NEEDS_DOCUMENTS",
        "report": report,
        "status": "PRIOR_AUTH_NEEDS_DOCUMENTS",
    }


def build_prior_auth_request_node(state: PriorAuthState) -> PriorAuthState:
    claim = ClaimForPriorAuth.model_validate(state["claim_input"])
    request_format, jurisdiction = infer_prior_auth_format(claim)
    request = build_canonical_prior_auth_request(
        claim=claim,
        requirements=state.get("requirements", []),
        request_format=request_format,
        jurisdiction=jurisdiction,
    )
    payload, payload_type = build_prior_auth_payload(
        request=request,
        claim=claim,
        request_format=request_format,
    )
    endpoint = prior_auth_endpoint_for_format(request_format)
    warnings = state.get("warnings", [])
    if request_format in {"NPHIES", "SHAFAFIYA", "ECLAIMLINK"}:
        warnings = add_warning(
            {**state, "warnings": warnings},
            "DRAFT_PRIOR_AUTH_ADAPTER",
            f"{request_format} prior authorization payload is a draft adapter and is not payer-certified.",
            {"format": request_format},
        )

    return {
        **state,
        "prior_auth_request": request,
        "request_format": request_format,
        "jurisdiction": jurisdiction,
        "request_payload": payload,
        "request_payload_type": payload_type,
        "submission_endpoint": endpoint,
        "warnings": warnings,
        "status": "PRIOR_AUTH_REQUEST_BUILT",
    }


def submit_prior_auth_node(state: PriorAuthState) -> PriorAuthState:
    endpoint = state.get("submission_endpoint", "")
    dry_run = state.get("dry_run", DEFAULT_PRIOR_AUTH_DRY_RUN)
    auto_submit = state.get("auto_submit", DEFAULT_PRIOR_AUTH_AUTO_SUBMIT)

    if not auto_submit:
        response = {
            "status": "prepared",
            "submitted": False,
            "dry_run": dry_run,
            "message": "Prior authorization payload is ready. auto_submit is false.",
        }
        return {
            **state,
            "submission_response": response,
            "routing": "SUBMISSION_READY",
            "status": "PRIOR_AUTH_SUBMISSION_READY",
        }

    if dry_run or not endpoint:
        simulated_status = normalize_token(state.get("simulated_response_status") or DEFAULT_PRIOR_AUTH_DRY_RUN_STATUS)
        response = dry_run_nphies_response(
            outcome=simulated_status,
            endpoint=endpoint,
            submitted=True,
        )
        return {
            **state,
            "submission_response": response,
            "status": "PRIOR_AUTH_SUBMITTED",
        }

    response = submit_payload(
        endpoint=endpoint,
        payload=state.get("request_payload"),
        payload_type=state.get("request_payload_type", "application/json"),
    )
    return {
        **state,
        "submission_response": response,
        "status": "PRIOR_AUTH_SUBMITTED",
    }


def poll_prior_auth_node(state: PriorAuthState) -> PriorAuthState:
    response = state.get("submission_response", {})
    status = normalize_response_status(response)
    if status not in {"queued", "pended", "partial"} or not state.get("poll_enabled"):
        return {**state, "poll_response": response}

    if state.get("dry_run", True):
        final_status = normalize_token(state.get("simulated_final_response_status") or status)
        final_decision = state.get("simulated_final_decision") or None
        poll_response = {
            **response,
            **dry_run_nphies_response(
                outcome=final_status,
                endpoint=response.get("endpoint", ""),
                submitted=True,
                decision=final_decision,
            ),
            "polled": True,
        }
        return {**state, "poll_response": poll_response}

    poll_response = poll_payload(response=response)
    return {**state, "poll_response": poll_response}


def store_prior_auth_reference_node(state: PriorAuthState) -> PriorAuthState:
    claim = dict(state.get("claim", {}))
    response = state.get("poll_response") or state.get("submission_response", {})
    response_status = normalize_response_status(response)
    response_outcome = extract_response_outcome(response) or response_status
    adjudication_mode = (
        "non_real_time"
        if response.get("polled") or normalize_token(response_outcome) in {"queued", "partial", "pended"}
        else adjudication_mode_for_status(response_status)
    )
    nphies_generated = is_nphies_generated_response(response)
    alternate_follow_up_required = response_status in {"pended", "failed"}
    pre_auth_ref = extract_pre_auth_ref(response)

    if response_status == "approved" and pre_auth_ref:
        prior_auth = {
            "ref": pre_auth_ref,
            "approved": True,
            "status": "approved",
            "approved_at": utc_now(),
            "procedure_codes": [item["procedure_code"] for item in state.get("requirements", [])],
            "valid_from": state.get("claim_input", {}).get("date_of_service"),
            "expires": response.get("expires") or response.get("valid_to"),
            "source": "PriorAuthorizationAgent",
        }
        claim["prior_auth"] = prior_auth
        if isinstance(claim.get("fhir_claim"), dict):
            claim["fhir_claim"]["preAuthRef"] = [pre_auth_ref]
        routing: PriorAuthRouting = "APPROVED"
        status: PriorAuthStatus = "PRIOR_AUTH_APPROVED"
    elif response_status == "denied":
        prior_auth = {
            "approved": False,
            "status": "denied",
            "source": "PriorAuthorizationAgent",
            "response_outcome": response_outcome,
        }
        claim["prior_auth"] = prior_auth
        routing = "DENIED"
        status = "PRIOR_AUTH_DENIED"
    elif response_status == "more_info_required":
        prior_auth = {
            "approved": False,
            "status": "more_info_required",
            "source": "PriorAuthorizationAgent",
            "response_outcome": response_outcome,
        }
        claim["prior_auth"] = prior_auth
        routing = "MORE_INFO_REQUIRED"
        status = "PRIOR_AUTH_MORE_INFO_REQUIRED"
    elif response_status == "partial":
        prior_auth = {
            "approved": False,
            "status": "partial",
            "source": "PriorAuthorizationAgent",
            "response_outcome": response_outcome,
            "polling_required": True,
        }
        claim["prior_auth"] = prior_auth
        routing = "PARTIAL"
        status = "PRIOR_AUTH_PARTIAL"
    elif response_status == "pended":
        prior_auth = {
            "approved": False,
            "status": "pended",
            "source": "PriorAuthorizationAgent",
            "response_outcome": response_outcome,
            "nphies_generated": nphies_generated,
            "polling_required": True,
            "alternate_follow_up_required": True,
        }
        claim["prior_auth"] = prior_auth
        routing = "PENDED_NPHIES"
        status = "PRIOR_AUTH_PENDED_NPHIES"
    elif response_status == "prepared":
        prior_auth = {
            "approved": False,
            "status": "prepared",
            "source": "PriorAuthorizationAgent",
            "response_outcome": response_outcome,
        }
        claim["prior_auth"] = prior_auth
        routing = "SUBMISSION_READY"
        status = "PRIOR_AUTH_SUBMISSION_READY"
    elif response_status == "failed":
        prior_auth = {
            "approved": False,
            "status": "failed",
            "source": "PriorAuthorizationAgent",
            "response_outcome": response_outcome,
            "alternate_follow_up_required": True,
        }
        claim["prior_auth"] = prior_auth
        routing = "FAILED"
        status = "PRIOR_AUTH_FAILED"
    else:
        prior_auth = {
            "approved": False,
            "status": "pending",
            "source": "PriorAuthorizationAgent",
            "response_outcome": response_outcome,
            "polling_required": True,
        }
        claim["prior_auth"] = prior_auth
        routing = "PENDING_PAYER"
        status = "PRIOR_AUTH_PENDING"

    report = {
        "claim_id": state.get("claim_input", {}).get("claim_id"),
        "routing": routing,
        "status": status,
        "request_format": state.get("request_format"),
        "jurisdiction": state.get("jurisdiction"),
        "payload_type": state.get("request_payload_type"),
        "submission_endpoint": state.get("submission_endpoint"),
        "dry_run": state.get("dry_run"),
        "auto_submit": state.get("auto_submit"),
        "response_outcome": response_outcome,
        "normalized_response_status": response_status,
        "adjudication_mode": adjudication_mode,
        "nphies_generated": nphies_generated,
        "polling_required": response_status in {"queued", "partial", "pended", "complete"},
        "alternate_follow_up_required": alternate_follow_up_required,
        "requirements": state.get("requirements", []),
        "missing_documents": state.get("missing_documents", []),
        "submission_response": state.get("submission_response", {}),
        "poll_response": state.get("poll_response", {}),
        "pre_auth_ref": pre_auth_ref,
        "updated_prior_auth": prior_auth,
        "warnings": state.get("warnings", []),
        "errors": state.get("errors", []),
        "events": state.get("prior_auth_events", []),
        "generated_at": utc_now(),
    }
    return {
        **state,
        "pre_auth_ref": pre_auth_ref or "",
        "response_outcome": response_outcome,
        "adjudication_mode": adjudication_mode,
        "nphies_generated": nphies_generated,
        "alternate_follow_up_required": alternate_follow_up_required,
        "updated_claim": claim,
        "routing": routing,
        "report": report,
        "status": status,
    }


def complete_existing_valid_node(state: PriorAuthState) -> PriorAuthState:
    claim = dict(state.get("claim", {}))
    existing_auth = state.get("existing_prior_auth", {})
    if existing_auth:
        claim["prior_auth"] = existing_auth
    report = {
        "claim_id": state.get("claim_input", {}).get("claim_id"),
        "routing": "ALREADY_VALID",
        "status": "PRIOR_AUTH_ALREADY_VALID",
        "existing_prior_auth": existing_auth,
        "requirements": state.get("requirements", []),
        "message": "Existing prior authorization covers all required procedure codes.",
        "generated_at": utc_now(),
    }
    return {
        **state,
        "updated_claim": claim,
        "routing": "ALREADY_VALID",
        "report": report,
        "status": "PRIOR_AUTH_ALREADY_VALID",
    }


def error_handler_node(state: PriorAuthState) -> PriorAuthState:
    report = {
        "claim_id": state.get("claim_input", {}).get("claim_id"),
        "routing": "FAILED",
        "status": "PRIOR_AUTH_FAILED",
        "errors": state.get("errors", []),
        "warnings": state.get("warnings", []),
        "events": state.get("prior_auth_events", []),
        "generated_at": utc_now(),
    }
    return {
        **state,
        "routing": "FAILED",
        "report": report,
        "status": "PRIOR_AUTH_FAILED",
    }


def route_after_step(state: PriorAuthState) -> str:
    return "error" if state.get("errors") else "continue"


def route_after_detect(state: PriorAuthState) -> str:
    if state.get("errors"):
        return "error"
    return "required" if state.get("requirements") else "not_required"


def route_after_existing_auth(state: PriorAuthState) -> str:
    if state.get("errors"):
        return "error"
    return "valid" if state.get("prior_auth_valid") else "missing"


def route_after_documents(state: PriorAuthState) -> str:
    if state.get("errors"):
        return "error"
    return "missing_documents" if state.get("missing_documents") else "ready"


NodeFunction = Callable[[PriorAuthState], PriorAuthState]


def safe_node(node_name: str, node_fn: NodeFunction) -> NodeFunction:
    def wrapped(state: PriorAuthState) -> PriorAuthState:
        if state.get("errors"):
            return with_event(state, node_name, "skipped", "Skipped because a previous error exists.")
        try:
            result = node_fn(state)
            if result.get("errors"):
                return with_event(result, node_name, "failed", "Node returned errors.")
            return with_event(result, node_name, "completed")
        except (ValidationError, Exception) as exc:
            failed_state: PriorAuthState = {
                **state,
                "status": "PRIOR_AUTH_FAILED",
                "errors": add_error(
                    state,
                    "PRIOR_AUTH_NODE_FAILED",
                    str(exc),
                    {"node": node_name},
                ),
            }
            return with_event(failed_state, node_name, "failed", str(exc))

    return wrapped


prior_auth_checkpointer = MemorySaver()


def build_prior_authorization_graph(*, checkpointer: Any | None = None):
    graph = StateGraph(PriorAuthState)

    graph.add_node("normalize_input", safe_node("normalize_input", normalize_input_node))
    graph.add_node(
        "detect_prior_auth_requirement",
        safe_node("detect_prior_auth_requirement", detect_prior_auth_requirement_node),
    )
    graph.add_node("complete_not_required", safe_node("complete_not_required", complete_not_required_node))
    graph.add_node(
        "validate_existing_prior_auth",
        safe_node("validate_existing_prior_auth", validate_existing_prior_auth_node),
    )
    graph.add_node("complete_existing_valid", safe_node("complete_existing_valid", complete_existing_valid_node))
    graph.add_node("collect_documents", safe_node("collect_documents", collect_documents_node))
    graph.add_node(
        "complete_needs_documents",
        safe_node("complete_needs_documents", complete_needs_documents_node),
    )
    graph.add_node("build_prior_auth_request", safe_node("build_prior_auth_request", build_prior_auth_request_node))
    graph.add_node("submit_prior_auth", safe_node("submit_prior_auth", submit_prior_auth_node))
    graph.add_node("poll_prior_auth", safe_node("poll_prior_auth", poll_prior_auth_node))
    graph.add_node("store_prior_auth_reference", safe_node("store_prior_auth_reference", store_prior_auth_reference_node))
    graph.add_node("error_handler", error_handler_node)

    graph.add_edge(START, "normalize_input")
    graph.add_conditional_edges(
        "normalize_input",
        route_after_step,
        {"continue": "detect_prior_auth_requirement", "error": "error_handler"},
    )
    graph.add_conditional_edges(
        "detect_prior_auth_requirement",
        route_after_detect,
        {
            "required": "validate_existing_prior_auth",
            "not_required": "complete_not_required",
            "error": "error_handler",
        },
    )
    graph.add_conditional_edges(
        "validate_existing_prior_auth",
        route_after_existing_auth,
        {
            "valid": "complete_existing_valid",
            "missing": "collect_documents",
            "error": "error_handler",
        },
    )
    graph.add_conditional_edges(
        "collect_documents",
        route_after_documents,
        {
            "missing_documents": "complete_needs_documents",
            "ready": "build_prior_auth_request",
            "error": "error_handler",
        },
    )
    graph.add_conditional_edges(
        "build_prior_auth_request",
        route_after_step,
        {"continue": "submit_prior_auth", "error": "error_handler"},
    )
    graph.add_conditional_edges(
        "submit_prior_auth",
        route_after_step,
        {"continue": "poll_prior_auth", "error": "error_handler"},
    )
    graph.add_conditional_edges(
        "poll_prior_auth",
        route_after_step,
        {"continue": "store_prior_auth_reference", "error": "error_handler"},
    )
    graph.add_edge("store_prior_auth_reference", END)
    graph.add_edge("complete_existing_valid", END)
    graph.add_edge("complete_needs_documents", END)
    graph.add_edge("complete_not_required", END)
    graph.add_edge("error_handler", END)

    return graph.compile(checkpointer=checkpointer if checkpointer is not None else prior_auth_checkpointer)


prior_authorization_agent = build_prior_authorization_graph()


def prior_auth_thread_id(initial_state: PriorAuthState | dict[str, Any]) -> str:
    raw_claim = claim_from_state(initial_state)
    claim_id = raw_claim.get("claim_id") or raw_claim.get("id") or "unknown-claim"
    return f"{DEFAULT_PRIOR_AUTH_THREAD_PREFIX}:{claim_id}:{uuid4().hex}"


def prior_auth_graph_config(thread_id: str | None = None) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id or f"{DEFAULT_PRIOR_AUTH_THREAD_PREFIX}:{uuid4().hex}"}}


def run_prior_authorization(
    initial_state: PriorAuthState | dict[str, Any],
    *,
    thread_id: str | None = None,
) -> PriorAuthState:
    state = dict(initial_state)
    effective_thread_id = thread_id or prior_auth_thread_id(state)
    state["thread_id"] = effective_thread_id
    return prior_authorization_agent.invoke(state, config=prior_auth_graph_config(effective_thread_id))


def example_input_state() -> PriorAuthState:
    return {
        "raw_claim": {
            "claim_id": "CLM-PA-001",
            "patient": {
                "id": "P001",
                "name": "Fatima Al-Mansoori",
                "member_id": "M-88921",
                "date_of_birth": "1988-04-19",
                "gender": "female",
            },
            "payer": {"id": "tawuniya_sa", "name": "Tawuniya", "coverage_id": "COV-001"},
            "provider": {"id": "D001", "facility_id": "FAC-001", "specialty": "Orthopaedics"},
            "encounter_id": "ENC-001",
            "service_date": "2026-06-10",
            "claim_format": "NPHIES",
            "jurisdiction": "KSA",
            "diagnoses": [{"system": "ICD-10", "code": "M17.11", "description": "Right knee osteoarthritis"}],
            "procedures": [{"system": "CPT", "code": "27447", "description": "Total knee arthroplasty"}],
            "line_items": [{"code": "27447", "description": "Total knee arthroplasty", "quantity": 1}],
            "amount": {"net": 45000.0, "currency": "SAR"},
            "attachments": [
                {"type": "operative_report", "name": "operative_report.pdf"},
                {"type": "preoperative_assessment", "name": "preop.pdf"},
                {"type": "surgical_consent", "name": "consent.pdf"},
            ],
            "prior_auth": {"approved": False, "status": "missing"},
        },
        "dry_run": True,
        "auto_submit": True,
        "simulated_response_status": "approved",
    }


if __name__ == "__main__":
    result = run_prior_authorization(example_input_state())
    print("\n=== Prior Authorization Status ===")
    print(result.get("status"))
    print("\n=== Routing ===")
    print(result.get("routing"))
    print("\n=== Report ===")
    pprint(result.get("report"))
