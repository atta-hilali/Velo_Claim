from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class KnowledgeStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"
    CONDITIONAL = "CONDITIONAL"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class KnowledgeResult:
    status: KnowledgeStatus
    query: str
    source: str
    entity_id: str | None = None
    payer_id: str | None = None
    plan_id: str | None = None
    diagnosis_code: str | None = None
    diagnosis_system: str | None = None
    procedure_code: str | None = None
    procedure_system: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.status not in {KnowledgeStatus.UNKNOWN, KnowledgeStatus.UNAVAILABLE}

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


def normalize_code_system(value: Any, *, diagnosis: bool = False) -> str:
    text = str(value or "").strip().upper()
    if "ICD-10-AM" in text:
        return "ICD-10-AM"
    if "ICD-10-CM" in text or "ICD10CM" in text:
        return "ICD-10-CM"
    if "ICD" in text:
        return "ICD-10" if diagnosis else text
    if "CDT" in text or "ADA.ORG" in text:
        return "CDT"
    if "HCPCS" in text:
        return "HCPCS"
    if "CPT" in text or "AMA-ASSN" in text:
        return "CPT"
    return text or ("ICD-10" if diagnosis else "UNKNOWN")


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return str(value)
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
