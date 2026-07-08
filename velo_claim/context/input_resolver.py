from __future__ import annotations

from typing import Any

from velo_claim.core.models import CanonicalState, default_state


class InputResolver:
    """Normalise all entrypoint styles into the canonical state shape."""

    def resolve(self, raw_input: dict[str, Any]) -> CanonicalState:
        state = {**default_state(), **raw_input}
        if "encounter_package" in raw_input and "source_context" not in raw_input:
            package = raw_input.get("encounter_package") or {}
            state["source_context"] = _source_context_from_package(package, raw_input)
        elif "source_context" not in raw_input and _looks_like_manual_encounter(raw_input):
            state["source_context"] = _source_context_from_package(raw_input, raw_input)
        return state


def _looks_like_manual_encounter(raw_input: dict[str, Any]) -> bool:
    return any(
        key in raw_input
        for key in (
            "patient",
            "coverage",
            "encounter",
            "provider",
            "facility",
            "conditions",
            "procedures",
            "charge_items",
        )
    )


def _source_context_from_package(package: dict[str, Any], raw_input: dict[str, Any]) -> dict[str, Any]:
    encounter = _normalize_encounter(package.get("encounter", {}), package)
    procedures = [_normalize_procedure(item) for item in package.get("procedures") or encounter.get("procedures", [])]
    return {
        "patient": _normalize_patient(package.get("patient", {})),
        "coverage": _normalize_coverage(package.get("coverage", {})),
        "encounter": encounter,
        "provider": _normalize_provider(package.get("provider", {})),
        "facility": _normalize_facility(package.get("facility", {})),
        "conditions": [_normalize_condition(item) for item in package.get("conditions") or encounter.get("conditions", [])],
        "procedures": procedures,
        "attachments": package.get("attachments") or encounter.get("attachments", []),
        "charge_items": _normalize_charge_items(package.get("charge_items") or encounter.get("charge_items", []), procedures),
        "payer_rules": package.get("payer_rules", raw_input.get("payer_rules", [])),
    }


def _normalize_patient(patient: dict[str, Any]) -> dict[str, Any]:
    if not patient:
        return {}
    normalized = dict(patient)
    if "identifier" not in normalized and "identifiers" in normalized:
        normalized["identifier"] = normalized.get("identifiers") or []
    if "identifier" not in normalized:
        identifiers = []
        if normalized.get("nationalId"):
            identifiers.append(
                {
                    "system": "ksa/national-id"
                    if str(normalized.get("idType", "")).lower() == "national_id"
                    else "national-id",
                    "value": normalized.get("nationalId"),
                    "type": normalized.get("idType"),
                }
            )
        if normalized.get("iqamaNumber"):
            identifiers.append(
                {
                    "system": "ksa/iqama",
                    "value": normalized.get("iqamaNumber"),
                    "type": "IQAMA",
                }
            )
        if normalized.get("memberId"):
            identifiers.append(
                {
                    "system": "velo/member-id",
                    "value": normalized.get("memberId"),
                    "type": "member_id",
                }
            )
        if identifiers:
            normalized["identifier"] = identifiers
    if "birthDate" not in normalized and normalized.get("birth_date"):
        normalized["birthDate"] = normalized["birth_date"]
    if "birthDate" not in normalized and normalized.get("dateOfBirth"):
        normalized["birthDate"] = normalized["dateOfBirth"]
    name = normalized.get("name")
    if isinstance(name, dict):
        given = name.get("given") or []
        family = name.get("family")
        normalized["name"] = [
            {
                "given": given if isinstance(given, list) else [given],
                "family": family,
                "text": " ".join(str(part) for part in [*(given if isinstance(given, list) else [given]), family] if part),
            }
        ]
    elif isinstance(name, str):
        normalized["name"] = [{"text": name}]
    elif normalized.get("firstName") or normalized.get("lastName"):
        given = [normalized["firstName"]] if normalized.get("firstName") else []
        family = normalized.get("lastName")
        normalized["name"] = [
            {
                "given": given,
                "family": family,
                "text": " ".join(str(part) for part in [*given, family] if part),
            }
        ]
    normalized["identifier"] = [
        _normalize_identifier(identifier)
        for identifier in normalized.get("identifier", [])
        if isinstance(identifier, dict)
    ]
    return normalized


def _normalize_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    if not coverage:
        return {}
    normalized = dict(coverage)
    normalized.setdefault("status", "active")
    if "subscriberId" not in normalized:
        normalized["subscriberId"] = (
            normalized.get("member_id")
            or normalized.get("memberId")
            or normalized.get("subscriber_id")
        )
    if "payor" not in normalized:
        normalized["payor"] = [
            {
                "identifier": {"value": normalized.get("payer_id") or normalized.get("payerId")},
                "display": normalized.get("payer_name") or normalized.get("payer"),
            }
        ]
    class_value = normalized.get("class")
    if not isinstance(class_value, list):
        normalized["class"] = [
            {
                "type": {"text": "plan"},
                "value": normalized.get("plan_id") or normalized.get("planId") or class_value,
                "name": normalized.get("plan_name") or normalized.get("planName") or normalized.get("planType"),
            }
        ]
    if "period" not in normalized and (normalized.get("coverage_start") or normalized.get("coverage_end")):
        normalized["period"] = {
            "start": normalized.get("coverage_start"),
            "end": normalized.get("coverage_end"),
        }
    if "period" not in normalized and (normalized.get("startDate") or normalized.get("endDate")):
        normalized["period"] = {
            "start": normalized.get("startDate"),
            "end": normalized.get("endDate"),
        }
    if "voi_verified" not in normalized and "voi_flag" in normalized:
        normalized["voi_verified"] = normalized.get("voi_flag")
    return normalized


def _normalize_provider(provider: dict[str, Any]) -> dict[str, Any]:
    if not provider:
        return {}
    normalized = dict(provider)
    normalized["name"] = _normalize_name(normalized.get("name"))
    if "identifier" not in normalized:
        identifiers = []
        if normalized.get("license"):
            identifiers.append(
                {
                    "system": normalized.get("license_system") or "provider-license",
                    "value": normalized.get("license"),
                }
            )
        if normalized.get("licenseNumber"):
            identifiers.append(
                {
                    "system": "ksa/practitioner-license",
                    "value": normalized.get("licenseNumber"),
                }
            )
        if normalized.get("nphiesProviderId"):
            identifiers.append(
                {
                    "system": "nphies/practitioner-id",
                    "value": normalized.get("nphiesProviderId"),
                }
            )
        if identifiers:
            normalized["identifier"] = identifiers
    if "qualification" not in normalized and normalized.get("specialty"):
        normalized["qualification"] = [{"code": {"text": normalized.get("specialty")}}]
    return normalized


def _normalize_facility(facility: dict[str, Any]) -> dict[str, Any]:
    if not facility:
        return {}
    normalized = dict(facility)
    if "identifier" not in normalized:
        identifiers = []
        if normalized.get("license"):
            identifiers.append(
                {
                    "system": normalized.get("license_system") or "facility-license",
                    "value": normalized.get("license"),
                }
            )
        if normalized.get("nphiesFacilityId"):
            identifiers.append(
                {
                    "system": "nphies/provider-id",
                    "value": normalized.get("nphiesFacilityId"),
                }
            )
        if identifiers:
            normalized["identifier"] = identifiers
    return normalized


def _normalize_encounter(encounter: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    if not encounter:
        encounter = {"id": package.get("encounter_id")}
    normalized = dict(encounter)
    if isinstance(normalized.get("class"), str):
        normalized["class"] = {"code": normalized["class"]}
    if isinstance(normalized.get("type"), str):
        normalized["type"] = [{"text": normalized["type"]}]
    if "period" not in normalized and normalized.get("service_date"):
        normalized["period"] = {
            "start": normalized.get("service_date"),
            "end": normalized.get("service_date"),
        }
    if "period" not in normalized and (normalized.get("admissionDate") or normalized.get("dischargeDate")):
        normalized["period"] = {
            "start": normalized.get("admissionDate"),
            "end": normalized.get("dischargeDate") or normalized.get("admissionDate"),
        }
    if "subject" not in normalized and package.get("patient", {}).get("id"):
        normalized["subject"] = {"reference": f"Patient/{package['patient']['id']}"}
    if "serviceProvider" not in normalized and package.get("facility", {}).get("id"):
        normalized["serviceProvider"] = {"reference": f"Organization/{package['facility']['id']}"}
    if "participant" not in normalized and package.get("provider", {}).get("id"):
        normalized["participant"] = [
            {"individual": {"reference": f"Practitioner/{package['provider']['id']}"}}
        ]
    return normalized


def _normalize_condition(condition: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(condition)
    if isinstance(normalized.get("code"), str) and (normalized.get("codeSystem") or normalized.get("display")):
        normalized["code"] = {
            "coding": [
                {
                    "system": normalized.get("codeSystem") or "ICD-10",
                    "code": normalized.get("code"),
                    "display": normalized.get("display"),
                }
            ]
        }
    if "code" not in normalized and normalized.get("icd_code"):
        normalized["code"] = {
            "coding": [
                {
                    "system": f"ICD-{normalized.get('icd_version') or '10'}",
                    "code": normalized.get("icd_code"),
                    "display": normalized.get("description"),
                }
            ]
        }
    return normalized


def _normalize_procedure(procedure: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(procedure)
    if isinstance(normalized.get("code"), str) and (normalized.get("codeSystem") or normalized.get("display")):
        normalized["code"] = {
            "coding": [
                {
                    "system": normalized.get("codeSystem") or "CPT",
                    "code": normalized.get("code"),
                    "display": normalized.get("display"),
                }
            ]
        }
    if "code" not in normalized and normalized.get("cpt_code"):
        normalized["code"] = {
            "coding": [
                {
                    "system": "CPT",
                    "code": normalized.get("cpt_code"),
                    "display": normalized.get("description"),
                }
            ]
        }
    if "quantity" not in normalized and normalized.get("units"):
        normalized["quantity"] = normalized.get("units")
    if "performedDateTime" not in normalized and normalized.get("date"):
        normalized["performedDateTime"] = normalized.get("date")
    return normalized


def _normalize_charge_items(
    charge_items: list[dict[str, Any]],
    procedures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_items = []
    procedures_by_sequence = {
        item.get("sequence"): item
        for item in procedures
        if isinstance(item, dict) and item.get("sequence") is not None
    }
    for index, charge_item in enumerate(charge_items):
        item = dict(charge_item)
        missing_financial_fields = []
        procedure = procedures_by_sequence.get(item.get("reference_procedure"))
        if procedure:
            item.setdefault("code", procedure.get("cpt_code"))
            item.setdefault("description", procedure.get("description"))
            item.setdefault("quantity", procedure.get("quantity") or 1)
            item.setdefault("gross", procedure.get("gross") or item.get("amount"))
            item.setdefault("patient_share", procedure.get("patient_share") or 0.0)
            item.setdefault("net", procedure.get("net") or item.get("amount"))
            item.setdefault("service_date", procedure.get("service_date"))
        item.setdefault("id", f"ACT-{index + 1:03d}")
        if "quantity" not in item:
            item["quantity"] = item.get("units") or 1
        for source_field in ("unitPrice", "totalAmount", "coveredAmount", "patientResponsibility"):
            if source_field in item and item.get(source_field) in (None, ""):
                missing_financial_fields.append(source_field)
        if "gross" not in item:
            item["gross"] = item.get("totalAmount") or item.get("unitPrice") or item.get("amount")
        if "net" not in item:
            item["net"] = item.get("coveredAmount") or item.get("totalAmount") or item.get("amount")
        if "patient_share" not in item:
            item["patient_share"] = item.get("patientResponsibility") or 0.0
        if item.get("gross") is None:
            item["gross"] = 0.0
        if item.get("net") is None:
            item["net"] = 0.0
        if item.get("patient_share") is None:
            item["patient_share"] = 0.0
        if missing_financial_fields:
            item["missing_financial_fields"] = sorted(set(missing_financial_fields))
        normalized_items.append(item)
    return normalized_items


def _normalize_identifier(identifier: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(identifier)
    system = str(normalized.get("system") or "")
    id_type = str(normalized.get("type") or "").lower()
    if "emirates" in id_type and "uae/emirates-id" not in system:
        normalized["system"] = "uae/emirates-id"
    elif "member" in id_type and not system:
        normalized["system"] = "velo/member-id"
    return normalized


def _normalize_name(name: Any) -> list[dict[str, Any]]:
    if isinstance(name, list):
        return name
    if isinstance(name, dict):
        given = name.get("given") or []
        family = name.get("family")
        return [
            {
                "given": given if isinstance(given, list) else [given],
                "family": family,
                "text": name.get("text")
                or " ".join(str(part) for part in [*(given if isinstance(given, list) else [given]), family] if part),
            }
        ]
    if isinstance(name, str):
        return [{"text": name}]
    return []
