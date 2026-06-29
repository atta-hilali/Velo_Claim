from __future__ import annotations

from typing import Any

from velo_claim.context.adapters import AdapterInterface
from velo_claim.core.models import CanonicalState, RoutingContext, SourceContext
from velo_claim.core.utils import first_identifier, first_name, reference_id


def resolve_fhir_context(state: CanonicalState, adapter: AdapterInterface | None = None) -> CanonicalState:
    """Build source_context and routing_context once for downstream modules."""

    current = state.get("source_context") or {}
    encounter = current.get("encounter") or state.get("encounter") or {}
    encounter_id = state.get("encounter_id") or encounter.get("id")

    if adapter and not encounter and encounter_id:
        encounter = adapter.fetch_encounter(encounter_id) or {}

    patient = current.get("patient") or state.get("patient") or {}
    coverage = current.get("coverage") or state.get("coverage") or {}
    provider = current.get("provider") or state.get("provider") or {}
    facility = current.get("facility") or state.get("facility") or {}

    patient_id = state.get("patient_id") or reference_id(encounter.get("subject", {}).get("reference")) or patient.get("id")
    provider_id = _first_provider_id(encounter) or provider.get("id")
    facility_id = reference_id(encounter.get("serviceProvider", {}).get("reference")) or facility.get("id")

    if adapter:
        if not patient and patient_id:
            patient = adapter.fetch_patient(patient_id) or {}
        if not coverage and patient_id:
            coverage = adapter.fetch_coverage(patient_id, state.get("payer_id")) or {}
        if not provider and provider_id:
            provider = adapter.fetch_practitioner(provider_id) or {}
        if not facility and facility_id:
            facility = adapter.fetch_organization(facility_id) or {}

    patient_id = patient.get("id") or patient_id
    encounter_ref = f"Encounter/{encounter.get('id')}" if encounter.get("id") else None
    if adapter and patient_id:
        conditions = current.get("conditions") or adapter.search("Condition", _search_params(patient_id, encounter_ref))
        procedures = current.get("procedures") or adapter.search("Procedure", _search_params(patient_id, encounter_ref))
        attachments = current.get("attachments") or adapter.search("DocumentReference", _search_params(patient_id, encounter_ref))
        charge_items = current.get("charge_items") or adapter.search("ChargeItem", _search_params(patient_id, encounter_ref))
    else:
        conditions = current.get("conditions", [])
        procedures = current.get("procedures", [])
        attachments = current.get("attachments", [])
        charge_items = current.get("charge_items", [])

    source_context = SourceContext(
        patient=patient,
        coverage=coverage,
        encounter=encounter,
        provider=provider,
        facility=facility,
        conditions=conditions,
        procedures=procedures,
        attachments=attachments,
        charge_items=charge_items,
        payer_rules=current.get("payer_rules", state.get("payer_rules", [])),
    )
    routing_context = extract_routing_context(source_context, state)

    return {
        **state,
        "source_context": source_context.to_dict(),
        "routing_context": routing_context.to_dict(),
    }


def extract_routing_context(source_context: SourceContext, state: dict[str, Any]) -> RoutingContext:
    coverage = source_context.coverage
    facility = source_context.facility
    provider = source_context.provider
    patient = source_context.patient
    payer_id, payer_name = _coverage_payer(coverage)
    plan_id = _coverage_plan(coverage)
    facility_system, facility_license = _license(
        facility,
        [
            "doh/facility-license",
            "dha/facility-code",
            "nphies/provider-id",
            "ksa/provider-license",
        ],
    )
    provider_system, provider_license = _license(
        provider,
        [
            "doh/clinician-license",
            "dha/clinician-license",
            "nphies/practitioner-id",
            "ksa/practitioner-license",
        ],
    )
    jurisdiction_hint = state.get("jurisdiction") or _jurisdiction_from_license(facility_system, facility_license)
    currency = "SAR" if jurisdiction_hint == "KSA" else "AED"
    return RoutingContext(
        payer_id=state.get("payer_id") or payer_id or "UNKNOWN",
        payer_name=state.get("payer_name") or payer_name or "UNKNOWN",
        plan_id=state.get("plan_id") or plan_id or "UNKNOWN",
        jurisdiction_hint=jurisdiction_hint,
        facility_license_system=facility_system,
        facility_license=facility_license,
        provider_license_system=provider_system,
        provider_license=provider_license,
        currency=state.get("currency") or currency,
        patient_identifier_types=_patient_identifier_types(patient),
    )


def _first_provider_id(encounter: dict[str, Any]) -> str | None:
    for participant in encounter.get("participant", []):
        reference = participant.get("individual", {}).get("reference")
        if reference:
            return reference_id(reference)
    return None


def _search_params(patient_id: str, encounter_ref: str | None) -> dict[str, str]:
    params = {"patient": patient_id}
    if encounter_ref:
        params["encounter"] = encounter_ref
    return params


def _coverage_payer(coverage: dict[str, Any]) -> tuple[str | None, str | None]:
    payor = coverage.get("payor", [{}])[0] if coverage.get("payor") else {}
    return payor.get("identifier", {}).get("value"), payor.get("display") or first_name(payor)


def _coverage_plan(coverage: dict[str, Any]) -> str | None:
    for item in coverage.get("class", []):
        if item.get("type", {}).get("text", "").lower() == "plan":
            return item.get("value") or item.get("name")
    return coverage.get("plan_id") or coverage.get("planId")


def _license(resource: dict[str, Any], systems: list[str]) -> tuple[str | None, str | None]:
    for system in systems:
        value = first_identifier(resource, system)
        if value:
            return system, value
    identifiers = resource.get("identifier", [])
    if identifiers:
        return identifiers[0].get("system"), identifiers[0].get("value")
    return None, None


def _jurisdiction_from_license(system: str | None, value: str | None) -> str | None:
    text = f"{system or ''} {value or ''}".lower()
    if "nphies" in text or "ksa" in text:
        return "KSA"
    if "dha" in text or "dxb" in text:
        return "DUBAI"
    if "doh" in text or value and value.upper().startswith("MF"):
        return "ABU_DHABI"
    return None


def _patient_identifier_types(patient: dict[str, Any]) -> list[str]:
    kinds = []
    for identifier in patient.get("identifier", []):
        system = str(identifier.get("system", "")).lower()
        if "emirates" in system:
            kinds.append("emirates_id")
        if "iqama" in system:
            kinds.append("iqama")
        if "national" in system:
            kinds.append("national_id")
    return sorted(set(kinds))
