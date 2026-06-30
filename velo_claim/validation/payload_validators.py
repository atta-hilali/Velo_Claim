from __future__ import annotations

import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from velo_claim.core.enums import ClaimStandard, Severity
from velo_claim.core.models import CheckIssue, CheckResult


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
            shafafiya_xsd_path=os.getenv("SHAFAFIYA_CLAIM_XSD_PATH"),
            eclaimlink_xsd_path=os.getenv("ECLAIMLINK_CLAIM_XSD_PATH"),
            shafafiya_prior_request_xsd_path=os.getenv("SHAFAFIYA_PRIOR_REQUEST_XSD_PATH"),
            shafafiya_prior_authorization_xsd_path=os.getenv("SHAFAFIYA_PRIOR_AUTHORIZATION_XSD_PATH"),
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

        if bundle.get("resourceType") != "Bundle":
            issues.append(_error("NPHIES_BUNDLE_REQUIRED", "claim_payload.resourceType", "NPHIES payload must be a FHIR Bundle."))
        if bundle.get("type") != "message":
            issues.append(_error("NPHIES_MESSAGE_BUNDLE_REQUIRED", "claim_payload.type", "NPHIES Bundle.type must be message."))
        if self.config.nphies_profile_required and not bundle.get("meta", {}).get("profile"):
            issues.append(_error("NPHIES_BUNDLE_PROFILE_MISSING", "claim_payload.meta.profile", "NPHIES Bundle.meta.profile is required."))
        entries = [entry.get("resource", {}) for entry in bundle.get("entry", [])]
        if not entries or entries[0].get("resourceType") != "MessageHeader":
            issues.append(_error("NPHIES_MESSAGE_HEADER_FIRST", "claim_payload.entry[0]", "NPHIES MessageHeader must be the first Bundle entry."))
        claim = next((resource for resource in entries if resource.get("resourceType") == "Claim"), None)
        if not claim:
            issues.append(_error("NPHIES_CLAIM_MISSING", "claim_payload.entry", "NPHIES Bundle must contain a Claim resource."))
        elif self.config.nphies_profile_required and not claim.get("meta", {}).get("profile"):
            issues.append(_error("NPHIES_CLAIM_PROFILE_MISSING", "Claim.meta.profile", "NPHIES Claim.meta.profile is required."))
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
