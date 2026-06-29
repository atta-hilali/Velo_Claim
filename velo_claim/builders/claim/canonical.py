from __future__ import annotations

from typing import Any
from uuid import uuid4

from velo_claim.core.models import RoutingContext, SourceContext
from velo_claim.core.utils import first_codeable_text, first_identifier, first_name, normalize_code
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.engine import bundled_codes_for_code
from velo_claim.core.models import PayerRuleSet


def build_canonical_claim(
    *,
    claim_id: str | None,
    source_context: SourceContext | dict[str, Any],
    routing_context: RoutingContext | dict[str, Any],
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
) -> dict[str, Any]:
    source = source_context if isinstance(source_context, SourceContext) else SourceContext(**source_context)
    routing = routing_context if isinstance(routing_context, RoutingContext) else RoutingContext(**routing_context)
    effective_claim_id = claim_id or f"CLM-{uuid4().hex[:12].upper()}"
    diagnoses = _diagnoses(source)
    procedures = _procedures(source, payer_rules, kg_client)
    line_items = _line_items(source, procedures, routing.currency)
    gross = round(sum(float(item.get("gross", item.get("net", 0.0))) for item in line_items), 2)
    patient_share = round(sum(float(item.get("patient_share", 0.0)) for item in line_items), 2)
    net = round(sum(float(item.get("net", 0.0)) for item in line_items), 2)
    return {
        "claim_id": effective_claim_id,
        "patient": {
            "id": source.patient.get("id"),
            "name": first_name(source.patient),
            "member_id": source.coverage.get("subscriberId") or first_identifier(source.patient, "velo/member-id"),
            "emirates_id": first_identifier(source.patient, "uae/emirates-id", "emirates-id"),
            "birth_date": source.patient.get("birthDate"),
            "gender": source.patient.get("gender"),
        },
        "payer": {
            "id": routing.payer_id,
            "name": routing.payer_name,
            "plan_id": routing.plan_id,
            "coverage_id": source.coverage.get("id"),
            "coverage_status": source.coverage.get("status"),
            "coverage_period": source.coverage.get("period", {}),
        },
        "provider": {
            "id": source.provider.get("id"),
            "name": first_name(source.provider),
            "license": routing.provider_license,
            "license_system": routing.provider_license_system,
            "facility_id": source.facility.get("id"),
            "facility_name": source.facility.get("name"),
            "facility_license": routing.facility_license,
            "facility_license_system": routing.facility_license_system,
        },
        "encounter": {
            "id": source.encounter.get("id"),
            "type": first_codeable_text(source.encounter.get("type")) or source.encounter.get("class", {}).get("code"),
            "period": source.encounter.get("period", {}),
            "service_date": _service_date(source.encounter),
            "patient_id": source.encounter.get("patient_id") or source.patient.get("id"),
        },
        "diagnoses": diagnoses,
        "procedures": procedures,
        "line_items": line_items,
        "attachments": source.attachments,
        "amount": {
            "gross": gross,
            "patient_share": patient_share,
            "net": net,
            "currency": routing.currency,
        },
        "pre_auth_ref": None,
        "source_refs": {
            "patient": source.patient.get("id"),
            "coverage": source.coverage.get("id"),
            "encounter": source.encounter.get("id"),
        },
    }


def _diagnoses(source: SourceContext) -> list[dict[str, Any]]:
    diagnoses = []
    for index, condition in enumerate(source.conditions):
        code, display, system = _coding(condition.get("code"))
        if not code:
            code = condition.get("code")
            display = condition.get("description") or condition.get("display")
        if code:
            diagnoses.append(
                {
                    "system": _diagnosis_system(system),
                    "code": normalize_code(code),
                    "description": display or normalize_code(code),
                    "type": "principal" if index == 0 else "secondary",
                }
            )
    if not diagnoses:
        for item in source.encounter.get("diagnoses", []):
            if item.get("code"):
                diagnoses.append(
                    {
                        "system": item.get("system", "ICD-10"),
                        "code": normalize_code(item["code"]),
                        "description": item.get("description", item["code"]),
                        "type": item.get("type", "principal"),
                    }
                )
    return diagnoses


def _procedures(source: SourceContext, payer_rules: PayerRuleSet, kg_client: Neo4jClientInterface) -> list[dict[str, Any]]:
    procedures = []
    for procedure in source.procedures:
        code, display, system = _coding(procedure.get("code"))
        if not code:
            code = procedure.get("code")
            display = procedure.get("description") or procedure.get("display")
        if code:
            code = normalize_code(code)
            procedures.append(
                {
                    "system": _procedure_system(system),
                    "code": code,
                    "description": display or code,
                    "quantity": int(procedure.get("quantity") or procedure.get("units") or 1),
                    "service_date": procedure.get("performedDateTime") or _service_date(source.encounter),
                    "bundled_with": bundled_codes_for_code(
                        cpt_code=code,
                        payer_rules=payer_rules,
                        kg_client=kg_client,
                    ),
                }
            )
    if not procedures:
        for item in source.charge_items:
            code = normalize_code(item.get("code") or item.get("cpt") or item.get("procedure_code"))
            if code:
                procedures.append(
                    {
                        "system": item.get("system", "CPT"),
                        "code": code,
                        "description": item.get("description", code),
                        "quantity": int(item.get("quantity") or 1),
                        "service_date": item.get("service_date") or _service_date(source.encounter),
                        "bundled_with": bundled_codes_for_code(
                            cpt_code=code,
                            payer_rules=payer_rules,
                            kg_client=kg_client,
                        ),
                    }
                )
    return procedures


def _line_items(source: SourceContext, procedures: list[dict[str, Any]], currency: str) -> list[dict[str, Any]]:
    lines = []
    for index, charge in enumerate(source.charge_items):
        amount = float(charge.get("amount") or charge.get("net") or charge.get("gross") or 0.0)
        code = normalize_code(charge.get("code") or charge.get("cpt") or (procedures[index]["code"] if index < len(procedures) else ""))
        lines.append(
            {
                "id": charge.get("id") or f"ACT-{index + 1:03d}",
                "code": code,
                "system": charge.get("system", "CPT"),
                "description": charge.get("description") or code,
                "quantity": int(charge.get("quantity") or 1),
                "gross": float(charge.get("gross", amount)),
                "patient_share": float(charge.get("patient_share", 0.0)),
                "net": float(charge.get("net", amount)),
                "currency": charge.get("currency") or currency,
            }
        )
    if lines:
        return lines
    return [
        {
            "id": f"ACT-{index + 1:03d}",
            "code": proc["code"],
            "system": proc.get("system", "CPT"),
            "description": proc.get("description", proc["code"]),
            "quantity": proc.get("quantity", 1),
            "gross": 0.0,
            "patient_share": 0.0,
            "net": 0.0,
            "currency": currency,
        }
        for index, proc in enumerate(procedures)
    ]


def _coding(codeable: Any) -> tuple[str | None, str | None, str | None]:
    if isinstance(codeable, dict):
        coding = codeable.get("coding", [])
        if coding:
            first = coding[0]
            return first.get("code"), first.get("display"), first.get("system")
        return codeable.get("code"), codeable.get("text"), codeable.get("system")
    if isinstance(codeable, str):
        return codeable, None, None
    return None, None, None


def _diagnosis_system(system: str | None) -> str:
    if system and "icd" in system.lower():
        return system
    return "ICD-10"


def _procedure_system(system: str | None) -> str:
    if system and "ama-assn" in system.lower():
        return "CPT"
    return system or "CPT"


def _service_date(encounter: dict[str, Any]) -> str | None:
    period = encounter.get("period", {})
    return (period.get("start") or encounter.get("service_date") or "").split("T")[0] or None
