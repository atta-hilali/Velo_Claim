import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_text(stable_json(value))


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def reference_id(reference: str | None) -> str | None:
    if not reference:
        return None
    return str(reference).split("/")[-1]


def normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def first_identifier(resource: dict[str, Any], *systems: str) -> str | None:
    identifiers = resource.get("identifier", [])
    for system in systems:
        for identifier in identifiers:
            if identifier.get("system") == system:
                return identifier.get("value")
    return identifiers[0].get("value") if identifiers else None


def first_name(resource: dict[str, Any]) -> str | None:
    names = resource.get("name", [])
    if not names:
        return resource.get("name") if isinstance(resource.get("name"), str) else None
    first = names[0]
    if first.get("text"):
        return first["text"]
    given = first.get("given", [])
    family = first.get("family")
    parts = [*given, family] if isinstance(given, list) else [given, family]
    return " ".join(str(part) for part in parts if part) or None


def first_codeable_text(codeable: Any) -> str | None:
    if isinstance(codeable, list):
        codeable = codeable[0] if codeable else None
    if not codeable:
        return None
    if isinstance(codeable, str):
        return codeable
    if codeable.get("text"):
        return codeable["text"]
    coding = codeable.get("coding", [])
    if coding:
        return coding[0].get("display") or coding[0].get("code")
    return None
