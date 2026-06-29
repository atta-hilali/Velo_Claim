from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class PACanonicalForm:
    claim_id: str
    patient: dict[str, Any]
    coverage: dict[str, Any]
    provider: dict[str, Any]
    facility: dict[str, Any]
    service_date: str | None
    procedures: list[dict[str, Any]]
    diagnoses: list[str]
    supporting_docs: list[str] = field(default_factory=list)
    payer_id: str = "UNKNOWN"
    plan_id: str = "UNKNOWN"
    currency: str = "AED"
    pre_auth_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_pa_canonical_form(state: dict[str, Any], required_codes: list[str]) -> PACanonicalForm:
    claim = state.get("canonical_claim", {})
    required = set(required_codes)
    procedures = [proc for proc in claim.get("procedures", []) if proc.get("code") in required]
    if not procedures:
        procedures = [line for line in claim.get("line_items", []) if line.get("code") in required]
    return PACanonicalForm(
        claim_id=claim.get("claim_id") or state.get("claim", {}).get("claim_id"),
        patient=claim.get("patient", {}),
        coverage=claim.get("payer", {}),
        provider=claim.get("provider", {}),
        facility={
            "id": claim.get("provider", {}).get("facility_id"),
            "license": claim.get("provider", {}).get("facility_license"),
            "name": claim.get("provider", {}).get("facility_name"),
        },
        service_date=claim.get("encounter", {}).get("service_date"),
        procedures=procedures,
        diagnoses=[item.get("code") for item in claim.get("diagnoses", []) if item.get("code")],
        supporting_docs=[
            attachment.get("object_uri") or attachment.get("url") or attachment.get("name")
            for attachment in claim.get("attachments", [])
            if isinstance(attachment, dict)
        ],
        payer_id=claim.get("payer", {}).get("id", "UNKNOWN"),
        plan_id=claim.get("payer", {}).get("plan_id", "UNKNOWN"),
        currency=claim.get("amount", {}).get("currency", "AED"),
        pre_auth_ref=claim.get("pre_auth_ref"),
    )
