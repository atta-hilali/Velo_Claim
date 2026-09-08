from __future__ import annotations

import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from velo_claim.core.enums import ClaimStandard, Severity
from velo_claim.core.models import CheckIssue, CheckResult


DEFAULT_SHAFAFIYA_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "data" / "schemas" / "shafafiya" / "v2.0"


@dataclass(slots=True)
class PayloadValidationConfig:
    nphies_profile_required: bool = True
    nphies_fhir_validator_command: str | None = None
    nphies_fhir_validator_timeout_seconds: int = 60
    shafafiya_xsd_path: str | None = None
    eclaimlink_xsd_path: str | None = None
    shafafiya_prior_request_xsd_path: str | None = None
    shafafiya_prior_authorization_xsd_path: str | None = None

    @classmethod
    def from_env(cls) -> "PayloadValidationConfig":
        return cls(
            nphies_profile_required=os.getenv("NPHIES_PROFILE_REQUIRED", "true").lower() in {"1", "true", "yes"},
            nphies_fhir_validator_command=os.getenv("NPHIES_FHIR_VALIDATOR_COMMAND") or os.getenv("FHIR_VALIDATOR_COMMAND"),
            nphies_fhir_validator_timeout_seconds=int(os.getenv("NPHIES_FHIR_VALIDATOR_TIMEOUT_SECONDS", "60")),
            shafafiya_xsd_path=os.getenv("SHAFAFIYA_CLAIM_XSD_PATH") or str(DEFAULT_SHAFAFIYA_SCHEMA_DIR / "ClaimSubmission.xsd"),
            eclaimlink_xsd_path=os.getenv("ECLAIMLINK_CLAIM_XSD_PATH"),
            shafafiya_prior_request_xsd_path=os.getenv("SHAFAFIYA_PRIOR_REQUEST_XSD_PATH") or str(DEFAULT_SHAFAFIYA_SCHEMA_DIR / "PriorRequest.xsd"),
            shafafiya_prior_authorization_xsd_path=os.getenv("SHAFAFIYA_PRIOR_AUTHORIZATION_XSD_PATH") or str(DEFAULT_SHAFAFIYA_SCHEMA_DIR / "PriorAuthorization.xsd"),
        )


class PayloadValidator:
    """Standard-aware payload validator.

    NPHIES is validated structurally and can run an external FHIR validator
    command when configured. XML standards use XSD validation when an XSD path
    is configured; otherwise a visible warning is returned.
    """

    def __init__(self, config: PayloadValidationConfig | None = None) -> None:
        self.config = config or PayloadValidationConfig.from_env()

    def validate(self, *, payload: str, payload_type: str, route: dict[str, Any]) -> tuple[Any | None, CheckResult]:
        standard = ClaimStandard(route.get("claim_standard"))
        if payload_type == "fhir_bundle_json":
            return self._validate_nphies(payload)
        parsed = self._parse_xml(payload, expected_root="Claim.Submission")
        if parsed[1].issues:
            return parsed
        xml_root = parsed[0]
        xsd_path = self.config.shafafiya_xsd_path if standard == ClaimStandard.SHAFAFIYA else self.config.eclaimlink_xsd_path
        issues = self._validate_xml_xsd(payload, xsd_path, standard)
        return xml_root, CheckResult("PAYLOAD_CONFORMITY", "PASS" if not issues else "REVIEW", issues)

    def validate_prior_request(
        self,
        *,
        payload: str,
        payload_type: str,
        route: dict[str, Any],
    ) -> tuple[Any | None, CheckResult]:
        standard = ClaimStandard(route.get("prior_auth_standard") or route.get("claim_standard"))
        if payload_type == "fhir_bundle_json":
            return self._validate_nphies_prior_auth(payload)
        parsed = self._parse_xml(payload, expected_root="Prior.Request", check_type="PA_PAYLOAD_CONFORMITY")
        if parsed[1].issues:
            return parsed
        xsd_path = self.config.shafafiya_prior_request_xsd_path if standard == ClaimStandard.SHAFAFIYA else None
        issues = self._validate_xml_xsd(payload, xsd_path, standard, check_type="PA_PAYLOAD_CONFORMITY")
        return parsed[0], CheckResult("PA_PAYLOAD_CONFORMITY", "PASS" if not issues else "REVIEW", issues)

    def validate_prior_authorization_response(
        self,
        *,
        payload: str,
        payload_type: str,
        route: dict[str, Any],
    ) -> tuple[Any | None, CheckResult]:
        standard = ClaimStandard(route.get("prior_auth_standard") or route.get("claim_standard"))
        if payload_type == "fhir_bundle_json":
            return self._validate_nphies(payload)
        parsed = self._parse_xml(payload, expected_root="Prior.Authorization", check_type="PA_RESPONSE_CONFORMITY")
        if parsed[1].issues:
            return parsed
        xsd_path = self.config.shafafiya_prior_authorization_xsd_path if standard == ClaimStandard.SHAFAFIYA else None
        issues = self._validate_xml_xsd(payload, xsd_path, standard, check_type="PA_RESPONSE_CONFORMITY")
        return parsed[0], CheckResult("PA_RESPONSE_CONFORMITY", "PASS" if not issues else "REVIEW", issues)

    def validate_eligibility_request(
        self,
        *,
        payload: str,
        payload_type: str,
        route: dict[str, Any],
    ) -> tuple[Any | None, CheckResult]:
        standard = ClaimStandard(route.get("eligibility_standard") or route.get("claim_standard"))
        if payload_type == "fhir_bundle_json":
            return self._validate_nphies_eligibility_request(payload)
        parsed = self._parse_xml(payload, expected_root="Prior.Request", check_type="ELIGIBILITY_PAYLOAD_CONFORMITY")
        if parsed[1].issues:
            return parsed
        xsd_path = self.config.shafafiya_prior_request_xsd_path if standard == ClaimStandard.SHAFAFIYA else None
        issues = self._validate_xml_xsd(payload, xsd_path, standard, check_type="ELIGIBILITY_PAYLOAD_CONFORMITY")
        return parsed[0], CheckResult("ELIGIBILITY_PAYLOAD_CONFORMITY", _issue_status(issues), issues)

    def _validate_nphies(self, payload: str) -> tuple[dict[str, Any] | None, CheckResult]:
        issues: list[CheckIssue] = []
        try:
            bundle = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, CheckResult("PAYLOAD_CONFORMITY", "FAILED", [_critical("PAYLOAD_PARSE_FAILED", "claim_payload", str(exc))])

        issues.extend(
            _validate_nphies_claim_structure(
                bundle,
                require_profiles=self.config.nphies_profile_required,
            )
        )
        issues.extend(self._validate_nphies_profile_with_command(payload))
        return bundle, CheckResult("PAYLOAD_CONFORMITY", _issue_status(issues), issues)

    def _parse_xml(
        self,
        payload: str,
        *,
        expected_root: str,
        check_type: str = "PAYLOAD_CONFORMITY",
    ) -> tuple[ET.Element | None, CheckResult]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            return None, CheckResult(check_type, "FAILED", [_critical("PAYLOAD_PARSE_FAILED", "payload", str(exc), check_type=check_type)])
        issues = []
        if root.tag != expected_root:
            issues.append(_error("XML_ROOT_MISMATCH", "payload.root", f"XML root must be {expected_root}.", check_type=check_type))
        return root, CheckResult(check_type, "PASS" if not issues else "FAILED", issues)

    def _validate_xml_xsd(
        self,
        payload: str,
        xsd_path: str | None,
        standard: ClaimStandard,
        *,
        check_type: str = "PAYLOAD_CONFORMITY",
    ) -> list[CheckIssue]:
        if not xsd_path:
            return [
                CheckIssue(
                    code="XSD_NOT_CONFIGURED",
                    severity=Severity.WARNING,
                    check_type=check_type,
                    field="schema",
                    message=f"{standard} XSD path is not configured; only XML parse checks were run.",
                    suggestion="Set the relevant XSD path in .env.",
                    penalty=5,
                )
            ]
        path = Path(xsd_path)
        if not path.exists():
            return [_error("XSD_NOT_FOUND", "schema", f"Configured XSD does not exist: {xsd_path}", check_type=check_type)]
        try:
            import lxml.etree as LET
        except ImportError:
            return [_error("XSD_VALIDATOR_MISSING", "schema", "Install lxml to run XSD validation.", check_type=check_type)]
        try:
            schema_doc = LET.parse(str(path))
            schema = LET.XMLSchema(schema_doc)
            xml_doc = LET.fromstring(payload.encode("utf-8"))
        except LET.XMLSchemaParseError as exc:
            return [_error("XSD_SCHEMA_INVALID", "schema", str(exc), check_type=check_type)]
        except LET.XMLSyntaxError as exc:
            return [_critical("PAYLOAD_PARSE_FAILED", "payload", str(exc), check_type=check_type)]
        except OSError as exc:
            return [_error("XSD_LOAD_FAILED", "schema", str(exc), check_type=check_type)]

        if not schema.validate(xml_doc):
            message = "; ".join(str(item) for item in schema.error_log)
            return [_error("XSD_VALIDATION_FAILED", "payload", message, check_type=check_type)]
        return []

    def _validate_nphies_profile_with_command(
        self,
        payload: str,
        *,
        check_type: str = "PAYLOAD_CONFORMITY",
    ) -> list[CheckIssue]:
        command_template = self.config.nphies_fhir_validator_command
        if not command_template:
            return [
                CheckIssue(
                    code="FHIR_VALIDATOR_NOT_CONFIGURED",
                    severity=Severity.WARNING,
                    check_type=check_type,
                    field="nphies_profile",
                    message="NPHIES FHIR profile validator command is not configured; local structural checks were run.",
                    suggestion="Set NPHIES_FHIR_VALIDATOR_COMMAND with a {payload} placeholder for the payload file.",
                    penalty=5,
                )
            ]

        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as temp_file:
                temp_file.write(payload)
                temp_path = temp_file.name
            command = command_template.replace("{payload}", temp_path)
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.config.nphies_fhir_validator_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return [_error("FHIR_VALIDATOR_TIMEOUT", "nphies_profile", str(exc), check_type=check_type)]
        except Exception as exc:
            return [_error("FHIR_VALIDATOR_FAILED", "nphies_profile", str(exc), check_type=check_type)]
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            return [_error("FHIR_PROFILE_VALIDATION_FAILED", "claim_payload", output[:2000], check_type=check_type)]
        return []

    def _validate_nphies_prior_auth(self, payload: str) -> tuple[dict[str, Any] | None, CheckResult]:
        check_type = "PA_PAYLOAD_CONFORMITY"
        try:
            bundle = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, CheckResult(check_type, "FAILED", [_critical("PAYLOAD_PARSE_FAILED", "pa_payload", str(exc), check_type=check_type)])

        issues: list[CheckIssue] = []
        if bundle.get("resourceType") != "Bundle":
            issues.append(_error("NPHIES_BUNDLE_REQUIRED", "pa_payload.resourceType", "NPHIES PA payload must be a FHIR Bundle.", check_type=check_type))
        if bundle.get("type") != "message":
            issues.append(_error("NPHIES_MESSAGE_BUNDLE_REQUIRED", "pa_payload.type", "NPHIES PA Bundle.type must be message.", check_type=check_type))
        if self.config.nphies_profile_required and not bundle.get("meta", {}).get("profile"):
            issues.append(_error("NPHIES_BUNDLE_PROFILE_MISSING", "pa_payload.meta.profile", "NPHIES Bundle.meta.profile is required.", check_type=check_type))

        entries = [entry.get("resource", {}) for entry in bundle.get("entry", [])]
        header = entries[0] if entries else {}
        if header.get("resourceType") != "MessageHeader":
            issues.append(_error("NPHIES_MESSAGE_HEADER_FIRST", "pa_payload.entry[0]", "NPHIES MessageHeader must be the first Bundle entry.", check_type=check_type))
        elif header.get("eventCoding", {}).get("code") != "priorauth-request":
            issues.append(_error("NPHIES_EVENT_CODE_MISMATCH", "MessageHeader.eventCoding.code", "NPHIES PA request eventCoding.code must be priorauth-request.", check_type=check_type))

        resource_types = [resource.get("resourceType") for resource in entries]
        for required in ("Claim", "Coverage", "Patient", "Practitioner", "Encounter"):
            if required not in resource_types:
                issues.append(_error("NPHIES_RESOURCE_MISSING", "pa_payload.entry", f"NPHIES PA request must contain {required}.", check_type=check_type))
        if resource_types.count("Organization") < 2:
            issues.append(_error("NPHIES_ORGANIZATIONS_MISSING", "pa_payload.entry", "NPHIES PA request must contain provider and insurer Organization resources.", check_type=check_type))

        claim = next((resource for resource in entries if resource.get("resourceType") == "Claim"), None)
        if claim and claim.get("use") != "preauthorization":
            issues.append(
                _error(
                    "NPHIES_PA_USE_MISMATCH",
                    "Claim.use",
                    "NPHIES PA payload Claim.use must be preauthorization.",
                    check_type=check_type,
                )
            )
        if self.config.nphies_profile_required:
            for resource in entries:
                if resource.get("resourceType") and not resource.get("meta", {}).get("profile"):
                    issues.append(
                        _error(
                            "NPHIES_RESOURCE_PROFILE_MISSING",
                            f"{resource.get('resourceType')}.meta.profile",
                            f"NPHIES {resource.get('resourceType')} resource requires meta.profile.",
                            check_type=check_type,
                        )
                    )
        issues.extend(self._validate_nphies_profile_with_command(payload, check_type=check_type))
        return bundle, CheckResult(check_type, _issue_status(issues), issues)

    def _validate_nphies_eligibility_request(self, payload: str) -> tuple[dict[str, Any] | None, CheckResult]:
        check_type = "ELIGIBILITY_PAYLOAD_CONFORMITY"
        try:
            bundle = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, CheckResult(check_type, "FAILED", [_critical("PAYLOAD_PARSE_FAILED", "eligibility_payload", str(exc), check_type=check_type)])

        issues: list[CheckIssue] = []
        if bundle.get("resourceType") != "Bundle":
            issues.append(_error("NPHIES_BUNDLE_REQUIRED", "eligibility_payload.resourceType", "NPHIES eligibility payload must be a FHIR Bundle.", check_type=check_type))
        if bundle.get("type") != "message":
            issues.append(_error("NPHIES_MESSAGE_BUNDLE_REQUIRED", "eligibility_payload.type", "NPHIES eligibility Bundle.type must be message.", check_type=check_type))
        if self.config.nphies_profile_required and not bundle.get("meta", {}).get("profile"):
            issues.append(_error("NPHIES_BUNDLE_PROFILE_MISSING", "eligibility_payload.meta.profile", "NPHIES Bundle.meta.profile is required.", check_type=check_type))

        entries = [entry.get("resource", {}) for entry in bundle.get("entry", [])]
        header = entries[0] if entries else {}
        if header.get("resourceType") != "MessageHeader":
            issues.append(_error("NPHIES_MESSAGE_HEADER_FIRST", "eligibility_payload.entry[0]", "NPHIES MessageHeader must be the first Bundle entry.", check_type=check_type))
        elif header.get("eventCoding", {}).get("code") != "eligibility-request":
            issues.append(_error("NPHIES_EVENT_CODE_MISMATCH", "MessageHeader.eventCoding.code", "NPHIES eligibility request eventCoding.code must be eligibility-request.", check_type=check_type))

        resource_types = [resource.get("resourceType") for resource in entries]
        for required in ("CoverageEligibilityRequest", "Coverage", "Patient"):
            if required not in resource_types:
                issues.append(_error("NPHIES_RESOURCE_MISSING", "eligibility_payload.entry", f"NPHIES eligibility request must contain {required}.", check_type=check_type))
        if resource_types.count("Organization") < 2:
            issues.append(_error("NPHIES_ORGANIZATIONS_MISSING", "eligibility_payload.entry", "NPHIES eligibility request must contain provider and insurer Organization resources.", check_type=check_type))

        if self.config.nphies_profile_required:
            for resource in entries:
                if resource.get("resourceType") and not resource.get("meta", {}).get("profile"):
                    issues.append(
                        _error(
                            "NPHIES_RESOURCE_PROFILE_MISSING",
                            f"{resource.get('resourceType')}.meta.profile",
                            f"NPHIES {resource.get('resourceType')} resource requires meta.profile.",
                            check_type=check_type,
                        )
                    )
        issues.extend(self._validate_nphies_profile_with_command(payload, check_type=check_type))
        return bundle, CheckResult(check_type, _issue_status(issues), issues)


def _validate_nphies_claim_structure(
    bundle: dict[str, Any],
    *,
    require_profiles: bool,
) -> list[CheckIssue]:
    issues: list[CheckIssue] = []
    if bundle.get("resourceType") != "Bundle":
        issues.append(_error("NPHIES_BUNDLE_REQUIRED", "claim_payload.resourceType", "NPHIES payload must be a FHIR Bundle."))
    if bundle.get("type") != "message":
        issues.append(_error("NPHIES_MESSAGE_BUNDLE_REQUIRED", "claim_payload.type", "NPHIES Bundle.type must be message."))
    if not bundle.get("id"):
        issues.append(_error("NPHIES_BUNDLE_ID_MISSING", "claim_payload.id", "NPHIES Bundle.id is required."))
    if not bundle.get("timestamp"):
        issues.append(_error("NPHIES_BUNDLE_TIMESTAMP_MISSING", "claim_payload.timestamp", "NPHIES Bundle.timestamp is required."))
    if require_profiles and not bundle.get("meta", {}).get("profile"):
        issues.append(_error("NPHIES_BUNDLE_PROFILE_MISSING", "claim_payload.meta.profile", "NPHIES Bundle.meta.profile is required."))

    entries = [entry.get("resource", {}) for entry in bundle.get("entry", []) if isinstance(entry, dict)]
    expected_order = [
        "MessageHeader",
        "Organization",
        "Organization",
        "Practitioner",
        "Patient",
        "Coverage",
        "Encounter",
        "Claim",
    ]
    actual_order = [resource.get("resourceType") for resource in entries]
    if actual_order != expected_order:
        issues.append(
            _error(
                "NPHIES_RESOURCE_ORDER_INVALID",
                "claim_payload.entry",
                f"NPHIES claim Bundle resources must be ordered as {expected_order}; found {actual_order}.",
            )
        )
    if require_profiles:
        for index, resource in enumerate(entries):
            if resource.get("resourceType") and not resource.get("meta", {}).get("profile"):
                issues.append(
                    _error(
                        "NPHIES_RESOURCE_PROFILE_MISSING",
                        f"claim_payload.entry[{index}].resource.meta.profile",
                        f"NPHIES {resource.get('resourceType')} requires meta.profile.",
                    )
                )

    by_type: dict[str, list[dict[str, Any]]] = {}
    for resource in entries:
        by_type.setdefault(str(resource.get("resourceType")), []).append(resource)
    header = _one(by_type, "MessageHeader")
    practitioner = _one(by_type, "Practitioner")
    patient = _one(by_type, "Patient")
    coverage = _one(by_type, "Coverage")
    encounter = _one(by_type, "Encounter")
    claim = _one(by_type, "Claim")
    organizations = by_type.get("Organization", [])

    if header:
        event = header.get("eventCoding", {})
        _require_equal(issues, event.get("system"), "http://nphies.sa/terminology/CodeSystem/ksa-message-events", "NPHIES_EVENT_SYSTEM_INVALID", "MessageHeader.eventCoding.system")
        _require_equal(issues, event.get("code"), "claim-request", "NPHIES_EVENT_CODE_INVALID", "MessageHeader.eventCoding.code")
        if not header.get("id"):
            issues.append(_error("NPHIES_MESSAGE_HEADER_ID_MISSING", "MessageHeader.id", "MessageHeader.id is required and must be regenerated per message."))
        _require_http_endpoint(issues, header.get("source", {}).get("endpoint"), "MessageHeader.source.endpoint")
        destination = (header.get("destination") or [{}])[0]
        _require_http_endpoint(issues, destination.get("endpoint"), "MessageHeader.destination[0].endpoint")
        _validate_license_identifier(issues, destination.get("receiver", {}).get("identifier", {}), "http://nphies.sa/license/payer-license", "MessageHeader.destination[0].receiver.identifier")
        _validate_license_identifier(issues, header.get("sender", {}).get("identifier", {}), "http://nphies.sa/license/provider-license", "MessageHeader.sender.identifier")

    if len(organizations) != 2:
        issues.append(_error("NPHIES_ORGANIZATION_COUNT_INVALID", "claim_payload.entry", "NPHIES claim Bundle requires exactly two Organization resources."))
    else:
        organization_codes = {
            coding.get("code")
            for organization in organizations
            for item in organization.get("type", [])
            for coding in item.get("coding", [])
        }
        if not {"prov", "ins"}.issubset(organization_codes):
            issues.append(_error("NPHIES_ORGANIZATION_TYPES_INVALID", "Organization.type", "Provider and insurer Organization type codings are required."))

    if practitioner:
        _validate_license_identifier(issues, _first_identifier(practitioner), "http://nphies.sa/license/practitioner-license", "Practitioner.identifier")
        _validate_human_name(issues, practitioner, "Practitioner.name")
    if patient:
        patient_identifier = _first_identifier(patient)
        _require_equal(issues, patient_identifier.get("system"), "http://nphies.sa/identifier/patient", "NPHIES_PATIENT_IDENTIFIER_SYSTEM_INVALID", "Patient.identifier.system")
        if not patient_identifier.get("value"):
            issues.append(_error("NPHIES_PATIENT_IDENTIFIER_MISSING", "Patient.identifier.value", "Patient national identifier is required."))
        _validate_human_name(issues, patient, "Patient.name")

    if coverage:
        relationship = _first_coding(coverage.get("relationship"))
        _require_equal(issues, relationship.get("system"), "http://terminology.hl7.org/CodeSystem/subscriber-relationship", "NPHIES_RELATIONSHIP_SYSTEM_INVALID", "Coverage.relationship.coding.system")
        _require_equal(issues, relationship.get("code"), "self", "NPHIES_RELATIONSHIP_CODE_INVALID", "Coverage.relationship.coding.code")
        for field in ("subscriber", "beneficiary"):
            if not coverage.get(field, {}).get("reference"):
                issues.append(_error("NPHIES_COVERAGE_REFERENCE_MISSING", f"Coverage.{field}", f"Coverage.{field} reference is required."))
        if not coverage.get("payor", [{}])[0].get("reference"):
            issues.append(_error("NPHIES_COVERAGE_PAYOR_MISSING", "Coverage.payor", "Coverage.payor Organization reference is required."))
        period = coverage.get("period") or {}
        if not period.get("start") or not period.get("end"):
            issues.append(_error("NPHIES_COVERAGE_PERIOD_INCOMPLETE", "Coverage.period", "Coverage.period.start and end are required."))

    if encounter:
        encounter_class = encounter.get("class", {})
        _require_equal(issues, encounter_class.get("system"), "http://terminology.hl7.org/CodeSystem/v3-ActCode", "NPHIES_ENCOUNTER_CLASS_SYSTEM_INVALID", "Encounter.class.system")
        if encounter_class.get("code") not in {"AMB", "EMER", "HH", "IMP", "SS", "VR"}:
            issues.append(_error("NPHIES_ENCOUNTER_CLASS_INVALID", "Encounter.class.code", "Encounter class must be AMB, EMER, HH, IMP, SS, or VR."))
        period = encounter.get("period") or {}
        if not period.get("start") or not period.get("end") or period.get("start") == period.get("end"):
            issues.append(_error("NPHIES_ENCOUNTER_PERIOD_INVALID", "Encounter.period", "Encounter period requires distinct start and end values."))
        if not encounter.get("participant", [{}])[0].get("individual", {}).get("reference"):
            issues.append(_error("NPHIES_ENCOUNTER_PARTICIPANT_MISSING", "Encounter.participant", "Encounter must reference the rendering Practitioner."))

    if claim:
        _require_equal(issues, claim.get("use"), "claim", "NPHIES_CLAIM_USE_INVALID", "Claim.use")
        profile = " ".join(claim.get("meta", {}).get("profile", []))
        if require_profiles and not any(name in profile for name in ("professional-claim", "institutional-claim", "oral-claim", "pharmacy-claim", "vision-claim")):
            issues.append(_error("NPHIES_CLAIM_PROFILE_INVALID", "Claim.meta.profile", "Claim must use a supported NPHIES claim-type profile."))
        _validate_claim_codings(issues, claim)
        _validate_claim_references(issues, claim, entries)

    return issues


def _one(by_type: dict[str, list[dict[str, Any]]], resource_type: str) -> dict[str, Any] | None:
    values = by_type.get(resource_type, [])
    if len(values) != 1:
        return None
    return values[0]


def _first_identifier(resource: dict[str, Any]) -> dict[str, Any]:
    identifiers = resource.get("identifier") or []
    return identifiers[0] if identifiers else {}


def _first_coding(codeable: dict[str, Any] | None) -> dict[str, Any]:
    coding = (codeable or {}).get("coding") or []
    return coding[0] if coding else {}


def _require_equal(issues: list[CheckIssue], actual: Any, expected: Any, code: str, field: str) -> None:
    if actual != expected:
        issues.append(_error(code, field, f"{field} must be {expected}; found {actual!r}."))


def _require_http_endpoint(issues: list[CheckIssue], value: Any, field: str) -> None:
    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        issues.append(_error("NPHIES_ENDPOINT_INVALID", field, f"{field} must be an absolute HTTP(S) endpoint."))


def _validate_license_identifier(issues: list[CheckIssue], identifier: dict[str, Any], system: str, field: str) -> None:
    _require_equal(issues, identifier.get("system"), system, "NPHIES_LICENSE_SYSTEM_INVALID", f"{field}.system")
    if identifier.get("use") != "official" or not identifier.get("value"):
        issues.append(_error("NPHIES_LICENSE_IDENTIFIER_INVALID", field, f"{field} requires use=official and a value."))


def _validate_human_name(issues: list[CheckIssue], resource: dict[str, Any], field: str) -> None:
    name = (resource.get("name") or [{}])[0]
    if not name.get("family") or not name.get("given"):
        issues.append(_error("NPHIES_HUMAN_NAME_INCOMPLETE", field, f"{field} requires family and given."))


def _validate_claim_codings(issues: list[CheckIssue], claim: dict[str, Any]) -> None:
    claim_type = _first_coding(claim.get("type"))
    priority = _first_coding(claim.get("priority"))
    _require_equal(issues, claim_type.get("system"), "http://terminology.hl7.org/CodeSystem/claim-type", "NPHIES_CLAIM_TYPE_SYSTEM_INVALID", "Claim.type.coding.system")
    _require_equal(issues, priority.get("system"), "http://terminology.hl7.org/CodeSystem/processpriority", "NPHIES_PRIORITY_SYSTEM_INVALID", "Claim.priority.coding.system")
    for index, diagnosis in enumerate(claim.get("diagnosis", [])):
        coding = _first_coding(diagnosis.get("diagnosisCodeableConcept"))
        diagnosis_type = _first_coding((diagnosis.get("type") or [{}])[0])
        _require_equal(issues, coding.get("system"), "http://hl7.org/fhir/sid/icd-10", "NPHIES_DIAGNOSIS_SYSTEM_INVALID", f"Claim.diagnosis[{index}].diagnosisCodeableConcept.coding.system")
        _require_equal(issues, diagnosis_type.get("system"), "http://nphies.sa/terminology/CodeSystem/diagnosis-type", "NPHIES_DIAGNOSIS_TYPE_SYSTEM_INVALID", f"Claim.diagnosis[{index}].type.coding.system")
    for index, item in enumerate(claim.get("item", [])):
        coding = _first_coding(item.get("productOrService"))
        if coding.get("system") != "http://www.ama-assn.org/go/cpt":
            issues.append(_error("NPHIES_PROCEDURE_SYSTEM_INVALID", f"Claim.item[{index}].productOrService.coding.system", "Professional claim service items must use the AMA CPT system."))
    total = claim.get("total") or {}
    if total.get("value") is None or total.get("currency") != "SAR":
        issues.append(_error("NPHIES_TOTAL_INVALID", "Claim.total", "Claim.total requires a value in SAR."))


def _validate_claim_references(issues: list[CheckIssue], claim: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    available = {f"{resource.get('resourceType')}/{resource.get('id')}" for resource in entries}
    references = [
        claim.get("patient", {}).get("reference"),
        claim.get("provider", {}).get("reference"),
        claim.get("insurer", {}).get("reference"),
        (claim.get("insurance") or [{}])[0].get("coverage", {}).get("reference"),
        (claim.get("careTeam") or [{}])[0].get("provider", {}).get("reference"),
    ]
    references.extend(
        extension.get("valueReference", {}).get("reference")
        for extension in claim.get("extension", [])
        if "encounter" in str(extension.get("url", ""))
    )
    for reference in references:
        if not reference or reference not in available:
            issues.append(_error("NPHIES_REFERENCE_UNRESOLVED", "Claim.reference", f"Claim reference {reference!r} does not resolve inside the Bundle."))


def _critical(code: str, field: str, message: str, *, check_type: str = "PAYLOAD_CONFORMITY") -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.CRITICAL,
        check_type=check_type,
        field=field,
        message=message,
        suggestion="Rebuild the payload before continuing.",
        penalty=100,
    )


def _error(code: str, field: str, message: str, *, check_type: str = "PAYLOAD_CONFORMITY") -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.ERROR,
        check_type=check_type,
        field=field,
        message=message,
        suggestion="Fix the payload builder or configured schema/profile.",
        penalty=20,
    )


def _issue_status(issues: list[CheckIssue]) -> str:
    if any(issue.severity in {Severity.CRITICAL, Severity.ERROR} for issue in issues):
        return "FAILED"
    return "REVIEW" if issues else "PASS"
