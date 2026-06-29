from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from velo_claim.core.utils import reference_id


class AdapterInterface(ABC):
    """Common FHIR adapter contract for Epic, Cerner, Nabidh, TrakCare/IRIS, etc."""

    @abstractmethod
    def authenticate(self) -> str | None: ...

    @abstractmethod
    def fetch_encounter(self, encounter_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def fetch_patient(self, patient_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def fetch_coverage(self, patient_id: str, payer_id: str | None = None) -> dict[str, Any] | None: ...

    @abstractmethod
    def fetch_practitioner(self, practitioner_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def fetch_organization(self, organization_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def search(self, resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class InMemoryFHIRAdapter(AdapterInterface):
    resources: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def authenticate(self) -> str | None:
        return None

    def fetch_encounter(self, encounter_id: str) -> dict[str, Any] | None:
        return self._read("Encounter", encounter_id)

    def fetch_patient(self, patient_id: str) -> dict[str, Any] | None:
        return self._read("Patient", patient_id)

    def fetch_coverage(self, patient_id: str, payer_id: str | None = None) -> dict[str, Any] | None:
        candidates = self.search("Coverage", {"patient": patient_id})
        if payer_id:
            for coverage in candidates:
                payor = coverage.get("payor", [{}])[0]
                identifier = payor.get("identifier", {}).get("value")
                if payer_id in {identifier, payor.get("display")}:
                    return coverage
        return candidates[0] if candidates else None

    def fetch_practitioner(self, practitioner_id: str) -> dict[str, Any] | None:
        return self._read("Practitioner", practitioner_id)

    def fetch_organization(self, organization_id: str) -> dict[str, Any] | None:
        return self._read("Organization", organization_id)

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]:
        values = list(self.resources.get(resource_type, {}).values())
        if resource_type in {"Condition", "Procedure", "DocumentReference", "ChargeItem", "Coverage"}:
            patient_id = reference_id(params.get("patient"))
            encounter_id = reference_id(params.get("encounter"))
            filtered = []
            for resource in values:
                subject = resource.get("subject", {}).get("reference") or resource.get("patient", {}).get("reference")
                context = resource.get("encounter", {}).get("reference") or resource.get("context", {}).get("reference")
                if patient_id and reference_id(subject) != patient_id:
                    continue
                if encounter_id and context and reference_id(context) != encounter_id:
                    continue
                filtered.append(resource)
            return filtered
        return values

    def _read(self, resource_type: str, resource_id: str | None) -> dict[str, Any] | None:
        if not resource_id:
            return None
        return self.resources.get(resource_type, {}).get(reference_id(resource_id) or resource_id)
