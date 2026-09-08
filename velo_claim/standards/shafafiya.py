from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any


ALLOWED_DISPOSITION_FLAGS = {
    "PRODUCTION",
    "TEST",
    "PTE_SUBMIT",
    "PTE_VALIDATE_ONLY",
    "PTE_RESPONSE",
    "PTE_SHADOW_NOT_FOR_PAYMENT_SUBMIT",
    "PTE_SHADOW_NOT_FOR_PAYMENT_VALIDATE_ONLY",
    "SHADOW_NOT_FOR_PAYMENT_SUBMIT",
    "SHADOW_NOT_FOR_PAYMENT_VALIDATE_ONLY",
}

ALLOWED_ENCOUNTER_TYPES = {
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "12",
    "13",
    "15",
    "41",
    "42",
}
ALLOWED_ENCOUNTER_START_TYPES = {str(value) for value in range(1, 9)}
ALLOWED_ENCOUNTER_END_TYPES = {str(value) for value in range(1, 8)}


def disposition_flag(*, response: bool = False) -> str:
    default = "PTE_RESPONSE" if response else "PTE_VALIDATE_ONLY"
    value = os.getenv("SHAFAFIYA_RESPONSE_DISPOSITION_FLAG" if response else "SHAFAFIYA_DISPOSITION_FLAG", default)
    value = str(value).strip().upper()
    if value not in ALLOWED_DISPOSITION_FLAGS:
        raise ValueError(f"Unsupported Shafafiya DispositionFlag: {value}")
    return value


def format_datetime(value: Any) -> str:
    if value in (None, ""):
        return datetime.now().strftime("%d/%m/%Y %H:%M")
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, f"{text}T00:00:00"):
        try:
            return datetime.fromisoformat(candidate).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return text


def format_date(value: Any) -> str:
    return format_datetime(value).split(" ", 1)[0]


def money(value: Any) -> str:
    return f"{float(value or 0.0):.2f}"


def activity_type_code(system: Any) -> str:
    token = re.sub(r"[^A-Z0-9]", "", str(system or "").upper())
    if "HCPCS" in token:
        return "4"
    if token in {"CPT", "AMA", "AMAASSNORGOCPT"} or "AMASSNORGOCPT" in token:
        return "3"
    if token in {"TRADEDRUG", "BRANDDRUG"}:
        return "5"
    if token in {"CDT", "DENTAL"}:
        return "6"
    if token in {"SERVICE", "SERVICECODE", "LOCAL", "LOCALCODE"}:
        return "8"
    if token in {"IRDRG", "DRG"}:
        return "9"
    if token in {"GENERICDRUG", "GENERICS"}:
        return "10"
    raise ValueError(f"Unsupported Shafafiya activity code system: {system!r}")


def encounter_type_code(encounter: dict[str, Any]) -> str:
    explicit = encounter.get("shafafiya_type") or encounter.get("type_code")
    if explicit is not None:
        value = str(explicit).strip()
        if value not in ALLOWED_ENCOUNTER_TYPES:
            raise ValueError(f"Unsupported Shafafiya Encounter.Type: {value}")
        return value

    class_code = str(encounter.get("class_code") or "").strip().upper()
    mapping = {
        "AMB": "1",
        "OUTPATIENT": "1",
        "EMER": "2",
        "EMERGENCY": "2",
        "IMP": "3",
        "INPATIENT": "3",
        "SS": "5",
        "DAYCASE": "5",
        "HH": "12",
        "HOME": "12",
        "VR": "1",
        "TELEMEDICINE": "1",
    }
    return mapping.get(class_code, "1")


def encounter_start_type_code(encounter: dict[str, Any]) -> str:
    return _optional_enum(encounter.get("start_type"), ALLOWED_ENCOUNTER_START_TYPES, "1", "Encounter.StartType")


def encounter_end_type_code(encounter: dict[str, Any]) -> str:
    return _optional_enum(encounter.get("end_type"), ALLOWED_ENCOUNTER_END_TYPES, "1", "Encounter.EndType")


def bounded_identifier(value: Any, *, max_length: int = 30) -> str:
    text = re.sub(r"\s+", "-", str(value or "").strip())
    if len(text) <= max_length:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8].upper()
    return f"{text[: max_length - 9]}-{digest}"


def authorization_request_id(facility_id: Any, claim_id: Any) -> str:
    facility = re.sub(r"[^A-Za-z0-9_.-]", "-", str(facility_id or "UNKNOWN"))
    claim = re.sub(r"[^A-Za-z0-9_.-]", "-", str(claim_id or "UNKNOWN"))
    return f"{facility}-PA-{claim}"


def parse_authorization_response(payload: str, *, claim_id: str | None = None, payer_id: str | None = None) -> dict[str, Any]:
    root = ET.fromstring(payload)
    if _local_name(root.tag) != "Prior.Authorization":
        raise ValueError("Shafafiya response root must be Prior.Authorization.")

    header = _child(root, "Header")
    authorization = _child(root, "Authorization")
    if header is None or authorization is None:
        raise ValueError("Shafafiya response requires Header and Authorization.")

    result = _text(authorization, "Result")
    result_token = str(result or "").strip().lower()
    denial_code = _text(authorization, "DenialCode")
    if result_token in {"yes", "y", "true", "1", "approved", "authorized", "authorised"}:
        status = "approved"
    elif result_token in {"no", "n", "false", "0", "denied", "rejected"} or denial_code:
        status = "denied"
    else:
        status = "unknown"

    activity_results = []
    for activity in _children(authorization, "Activity"):
        activity_results.append(
            {
                "id": _text(activity, "ID"),
                "type": _text(activity, "Type"),
                "code": _text(activity, "Code"),
                "quantity": _text(activity, "Quantity"),
                "net": _text(activity, "Net"),
                "list": _text(activity, "List"),
                "patient_share": _text(activity, "PatientShare"),
                "payment_amount": _text(activity, "PaymentAmount"),
                "denial_code": _text(activity, "DenialCode"),
            }
        )

    return {
        "transaction_status": "complete" if status in {"approved", "denied"} else status,
        "outcome": "complete" if status in {"approved", "denied"} else None,
        "decision": status,
        "result": result,
        "authorization_request_id": _text(authorization, "ID"),
        "pre_auth_ref": _text(authorization, "IDPayer"),
        "payer_id": _text(header, "SenderID") or payer_id,
        "provider_id": _text(header, "ReceiverID"),
        "claim_id": claim_id,
        "cpt_codes": [item["code"] for item in activity_results if item.get("code")],
        "valid_from": _text(authorization, "Start"),
        "valid_to": _text(authorization, "End"),
        "denial_code": denial_code,
        "message": _text(authorization, "Comments") or denial_code,
        "activity_results": activity_results,
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(parent: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in list(parent) if _local_name(item.tag) == name), None)


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in list(parent) if _local_name(item.tag) == name]


def _text(parent: ET.Element, name: str) -> str | None:
    element = _child(parent, name)
    value = (element.text or "").strip() if element is not None else ""
    return value or None


def _optional_enum(value: Any, allowed: set[str], default: str, field: str) -> str:
    if value in (None, ""):
        return default
    token = str(value).strip()
    if token not in allowed:
        raise ValueError(f"Unsupported Shafafiya {field}: {token}")
    return token
