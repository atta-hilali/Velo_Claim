from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any


class UnsafeCorrectionError(ValueError):
    """Raised when a correction attempts to modify an unsafe canonical field."""


_TOKEN = re.compile(r"(?:^|\.)([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")
_CODE_COLLECTIONS = {"procedures", "diagnoses", "line_items", "attachments"}
_PROTECTED_PREFIXES = (
    "canonical_claim.claim_id",
    "canonical_claim.payer",
    "canonical_claim.pre_auth_ref",
    "canonical_claim.authorization",
    "canonical_claim.eligibility",
    "canonical_claim.patient.id",
    "canonical_claim.patient.identifier",
    "canonical_claim.patient.identifiers",
    "canonical_claim.patient.emirates_id",
    "canonical_claim.patient.national_id",
    "canonical_claim.provider.id",
    "canonical_claim.provider.license",
    "canonical_claim.provider.facility_id",
    "canonical_claim.provider.facility_license",
    "canonical_claim.external",
    "canonical_claim.transaction",
)
_ALLOWED_PREFIXES = (
    "canonical_claim.amount",
    "canonical_claim.line_items",
    "canonical_claim.procedures",
    "canonical_claim.diagnoses",
    "canonical_claim.attachments",
    "canonical_claim.encounter",
    "canonical_claim.provider",
    "canonical_claim.patient",
)


def normalize_field_path(field: str | None, canonical_claim: dict[str, Any]) -> str:
    path = str(field or "").strip()
    if not path:
        return "canonical_claim"
    path = path.replace("/", ".")
    if not path.startswith("canonical_claim"):
        return path

    parts = path.split(".")
    if len(parts) >= 3 and parts[1] in _CODE_COLLECTIONS and "[" not in parts[1]:
        collection = canonical_claim.get(parts[1]) or []
        selector = parts[2]
        if selector and isinstance(collection, list):
            numeric_index = int(selector) if selector.isdigit() else None
            index = numeric_index if numeric_index is not None and numeric_index < len(collection) else None
            if index is None:
                index = next(
                    (
                        idx
                        for idx, item in enumerate(collection)
                        if isinstance(item, dict)
                        and selector in {str(item.get("code") or ""), str(item.get("id") or "")}
                    ),
                    None,
                )
            if index is not None:
                suffix = ".".join(parts[3:])
                return f"canonical_claim.{parts[1]}[{index}]" + (f".{suffix}" if suffix else "")
    return path


def parse_field_path(field_path: str) -> list[str | int]:
    if not field_path.startswith("canonical_claim"):
        raise UnsafeCorrectionError("Correction paths must be rooted at canonical_claim.")
    tokens: list[str | int] = []
    cursor = 0
    for match in _TOKEN.finditer(field_path):
        if match.start() != cursor:
            raise UnsafeCorrectionError(f"Invalid correction field path: {field_path}")
        tokens.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
        cursor = match.end()
    if cursor != len(field_path) or not tokens or tokens[0] != "canonical_claim":
        raise UnsafeCorrectionError(f"Invalid correction field path: {field_path}")
    return tokens[1:]


def is_protected_path(field_path: str) -> bool:
    if field_path in {"canonical_claim", "route", "routing_context", "claim_payload"}:
        return True
    return any(
        field_path == prefix
        or field_path.startswith(prefix + ".")
        or field_path.startswith(prefix + "[")
        for prefix in _PROTECTED_PREFIXES
    )


def validate_correction_path(field_path: str) -> list[str | int]:
    tokens = parse_field_path(field_path)
    if is_protected_path(field_path):
        raise UnsafeCorrectionError(f"Protected field cannot be corrected here: {field_path}")
    if not any(
        field_path == prefix
        or field_path.startswith(prefix + ".")
        or field_path.startswith(prefix + "[")
        for prefix in _ALLOWED_PREFIXES
    ):
        raise UnsafeCorrectionError(f"Field is outside the correction allow-list: {field_path}")
    return tokens


def get_canonical_value(canonical_claim: dict[str, Any], field_path: str) -> Any:
    current: Any = canonical_claim
    for token in parse_field_path(field_path):
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                raise UnsafeCorrectionError(f"List index does not exist: {field_path}")
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                raise UnsafeCorrectionError(f"Field does not exist: {field_path}")
            current = current[token]
    return deepcopy(current)


def values_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def validate_proposed_value(field_path: str, current_value: Any, proposed_value: Any) -> None:
    validate_correction_path(field_path)
    try:
        encoded = json.dumps(proposed_value, default=str)
    except (TypeError, ValueError) as exc:
        raise UnsafeCorrectionError("Correction value must be JSON serializable.") from exc
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise UnsafeCorrectionError("Correction value exceeds the 64 KiB safety limit.")
    if proposed_value is None:
        raise UnsafeCorrectionError("A correction cannot replace a field with null.")
    if current_value is not None and not isinstance(proposed_value, type(current_value)):
        numeric = isinstance(current_value, (int, float)) and not isinstance(current_value, bool)
        if not (numeric and isinstance(proposed_value, (int, float)) and not isinstance(proposed_value, bool)):
            raise UnsafeCorrectionError("Correction value has a different type from the current field.")
    if field_path.endswith(".code") and not re.fullmatch(r"[A-Za-z0-9.\-]{1,40}", str(proposed_value)):
        raise UnsafeCorrectionError("Code correction has an invalid format.")
    if field_path.endswith(".currency") and not re.fullmatch(r"[A-Z]{3}", str(proposed_value)):
        raise UnsafeCorrectionError("Currency must be a three-letter uppercase code.")


def apply_correction(
    canonical_claim: dict[str, Any],
    *,
    field_path: str,
    expected_old_value: Any,
    proposed_value: Any,
) -> dict[str, Any]:
    tokens = validate_correction_path(field_path)
    actual = get_canonical_value(canonical_claim, field_path)
    if not values_equal(actual, expected_old_value):
        raise UnsafeCorrectionError(f"Current value no longer matches suggestion for {field_path}.")
    validate_proposed_value(field_path, actual, proposed_value)

    updated = deepcopy(canonical_claim)
    target: Any = updated
    for token in tokens[:-1]:
        target = target[token]
    target[tokens[-1]] = deepcopy(proposed_value)
    return updated


def value_fingerprint(value: Any) -> str:
    from hashlib import sha256

    return sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()
