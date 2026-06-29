"""Phase 1 payer registry helpers for Velo Claim."""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PAYER_REGISTRY_PATH = PROJECT_ROOT / "data" / "payers" / "phase1_payers.json"


def compact_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


@lru_cache(maxsize=4)
def load_payer_registry(path: str | Path = DEFAULT_PAYER_REGISTRY_PATH) -> dict[str, Any]:
    registry_path = Path(path)
    if not registry_path.is_absolute():
        registry_path = PROJECT_ROOT / registry_path
    if not registry_path.exists():
        return {"payers": []}
    return json.loads(registry_path.read_text(encoding="utf-8"))


def payer_records(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = registry if registry is not None else load_payer_registry()
    return [item for item in data.get("payers", []) if isinstance(item, dict)]


def payer_alias_values(record: dict[str, Any]) -> set[str]:
    values = {
        record.get("canonical_id"),
        record.get("display_name"),
        record.get("legal_name"),
        *(record.get("aliases") or []),
    }
    return {str(value) for value in values if value}


def payer_alias_tokens(value: Any, registry: dict[str, Any] | None = None) -> set[str]:
    token = compact_token(value)
    tokens = {normalized_text(value), token}
    if not token:
        return set()
    for record in payer_records(registry):
        record_tokens = {compact_token(item) for item in payer_alias_values(record)}
        if token in record_tokens:
            for alias in payer_alias_values(record):
                tokens.add(normalized_text(alias))
                tokens.add(compact_token(alias))
            tokens.add(str(record.get("canonical_id")))
            tokens.add(compact_token(record.get("canonical_id")))
            break
    return {item for item in tokens if item}


def lookup_payer(*values: Any, registry: dict[str, Any] | None = None) -> dict[str, Any] | None:
    search_tokens = {compact_token(value) for value in values if compact_token(value)}
    if not search_tokens:
        return None
    for record in payer_records(registry):
        record_tokens = {compact_token(item) for item in payer_alias_values(record)}
        if search_tokens & record_tokens:
            return record
    return None


def payer_matches(rule_payer_id: Any, claim_payer_id: Any, registry: dict[str, Any] | None = None) -> bool:
    if normalized_text(rule_payer_id) in {"*", "default"}:
        return True
    return bool(payer_alias_tokens(rule_payer_id, registry) & payer_alias_tokens(claim_payer_id, registry))


def infer_standard_for_payer(
    *values: Any,
    jurisdiction: str | None = None,
    registry: dict[str, Any] | None = None,
) -> str | None:
    payer = lookup_payer(*values, registry=registry)
    if not payer:
        return None
    jurisdiction_token = compact_token(jurisdiction)
    standards = set(payer.get("standards") or [])
    if jurisdiction_token in {"ksa", "sa", "saudi", "saudiarabia"} and "NPHIES" in standards:
        return "NPHIES"
    if jurisdiction_token in {"abudhabi", "auh", "doh", "aeauh"} and "SHAFAFIYA" in standards:
        return "SHAFAFIYA"
    if jurisdiction_token in {"dubai", "dxb", "dha", "aedxb"} and "ECLAIMLINK" in standards:
        return "ECLAIMLINK"
    return payer.get("default_standard")


def jurisdiction_for_payer(
    *values: Any,
    registry: dict[str, Any] | None = None,
) -> str | None:
    payer = lookup_payer(*values, registry=registry)
    if not payer:
        return None
    return payer.get("default_jurisdiction")


def phase1_payer_summary(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    records = payer_records(registry)
    phase1 = [item for item in records if item.get("phase") == 1]
    phase2 = [item for item in records if item.get("phase") == 2]
    return {
        "total_payers": len(records),
        "phase1_payers": len(phase1),
        "phase2_payers": len(phase2),
        "phase1_by_country": {
            country: len([item for item in phase1 if item.get("country") == country])
            for country in sorted({item.get("country") for item in phase1 if item.get("country")})
        },
        "phase1_priority_1": [
            item["canonical_id"]
            for item in phase1
            if item.get("priority") == 1
        ],
        "standards": sorted(
            {
                standard
                for item in records
                for standard in item.get("standards", [])
            }
        ),
    }
