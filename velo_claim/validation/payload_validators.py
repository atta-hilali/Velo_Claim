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

    @classmethod
    def from_env(cls) -> "PayloadValidationConfig":
        return cls(
            nphies_profile_required=os.getenv("NPHIES_PROFILE_REQUIRED", "true").lower() in {"1", "true", "yes"},
            nphies_fhir_validator_command=os.getenv("NPHIES_FHIR_VALIDATOR_COMMAND") or os.getenv("FHIR_VALIDATOR_COMMAND"),
            nphies_fhir_validator_timeout_seconds=int(os.getenv("NPHIES_FHIR_VALIDATOR_TIMEOUT_SECONDS", "60")),
            shafafiya_xsd_path=os.getenv("SHAFAFIYA_CLAIM_XSD_PATH"),
            eclaimlink_xsd_path=os.getenv("ECLAIMLINK_CLAIM_XSD_PATH"),
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
        parsed = self._parse_xml(payload)
        if parsed[1].issues:
            return parsed
        xml_root = parsed[0]
        xsd_path = self.config.shafafiya_xsd_path if standard == ClaimStandard.SHAFAFIYA else self.config.eclaimlink_xsd_path
        issues = self._validate_xml_xsd(payload, xsd_path, standard)
        return xml_root, CheckResult("PAYLOAD_CONFORMITY", "PASS" if not issues else "REVIEW", issues)

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
        return bundle, CheckResult("PAYLOAD_CONFORMITY", "PASS" if not issues else "FAILED", issues)

    def _parse_xml(self, payload: str) -> tuple[ET.Element | None, CheckResult]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            return None, CheckResult("PAYLOAD_CONFORMITY", "FAILED", [_critical("PAYLOAD_PARSE_FAILED", "claim_payload", str(exc))])
        issues = []
        if root.tag != "Claim.Submission":
            issues.append(_error("XML_ROOT_MISMATCH", "claim_payload.root", "Claim XML root must be Claim.Submission."))
        return root, CheckResult("PAYLOAD_CONFORMITY", "PASS" if not issues else "FAILED", issues)

    def _validate_xml_xsd(self, payload: str, xsd_path: str | None, standard: ClaimStandard) -> list[CheckIssue]:
        if not xsd_path:
            return [
                CheckIssue(
                    code="XSD_NOT_CONFIGURED",
                    severity=Severity.WARNING,
                    check_type="PAYLOAD_CONFORMITY",
                    field="schema",
                    message=f"{standard} XSD path is not configured; only XML parse checks were run.",
                    suggestion="Set SHAFAFIYA_CLAIM_XSD_PATH or ECLAIMLINK_CLAIM_XSD_PATH.",
                    penalty=5,
                )
            ]
        path = Path(xsd_path)
        if not path.exists():
            return [_error("XSD_NOT_FOUND", "schema", f"Configured XSD does not exist: {xsd_path}")]
        try:
            import lxml.etree as LET
        except ImportError:
            return [_error("XSD_VALIDATOR_MISSING", "schema", "Install lxml to run XSD validation.")]
        schema_doc = LET.parse(str(path))
        schema = LET.XMLSchema(schema_doc)
        xml_doc = LET.fromstring(payload.encode("utf-8"))
        if not schema.validate(xml_doc):
            message = "; ".join(str(item) for item in schema.error_log)
            return [_error("XSD_VALIDATION_FAILED", "claim_payload", message)]
        return []

    def _validate_nphies_profile_with_command(self, payload: str) -> list[CheckIssue]:
        command_template = self.config.nphies_fhir_validator_command
        if not command_template:
            return [
                CheckIssue(
                    code="FHIR_VALIDATOR_NOT_CONFIGURED",
                    severity=Severity.WARNING,
                    check_type="PAYLOAD_CONFORMITY",
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
            return [_error("FHIR_VALIDATOR_TIMEOUT", "nphies_profile", str(exc))]
        except Exception as exc:
            return [_error("FHIR_VALIDATOR_FAILED", "nphies_profile", str(exc))]
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            return [_error("FHIR_PROFILE_VALIDATION_FAILED", "claim_payload", output[:2000])]
        return []


def _critical(code: str, field: str, message: str) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.CRITICAL,
        check_type="PAYLOAD_CONFORMITY",
        field=field,
        message=message,
        suggestion="Rebuild the payload before continuing.",
        penalty=100,
    )


def _error(code: str, field: str, message: str) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=Severity.ERROR,
        check_type="PAYLOAD_CONFORMITY",
        field=field,
        message=message,
        suggestion="Fix the payload builder or configured schema/profile.",
        penalty=20,
    )
