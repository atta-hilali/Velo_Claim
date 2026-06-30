from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.checks.prior_auth import auth_valid
from velo_claim.core.enums import PayloadStatus, PriorAuthStatus, Severity
from velo_claim.core.models import CheckIssue, CheckResult, PayerRuleSet
from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.engine import pa_required_for_code
from velo_claim.storage.interfaces import ObjectStoreInterface, RepositoryInterface


WAITING_RESPONSE_STATUSES = {"queued", "pending", "pended", "partial", "in-progress", "inprogress"}
APPROVED_RESPONSE_STATUSES = {"approved", "active", "authorized", "authorised"}
REFERENCE_ACCEPTED_RESPONSE_STATUSES = {"accepted"}
DENIED_RESPONSE_STATUSES = {"denied", "rejected", "declined", "cancelled", "canceled"}
ERROR_RESPONSE_STATUSES = {"error", "failed", "invalid"}
COMPLETE_RESPONSE_STATUSES = {"complete", "completed"}


def normalize_prior_auth_response(raw_response: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Normalize payer/NPHIES/Shafafiya PA responses into storage-ready data."""

    raw = _coerce_response(raw_response)
    claim = state.get("canonical_claim", {})
    payer = claim.get("payer", {})
    required_codes = [normalize_code(code) for code in state.get("prior_auth_required_codes", []) if code]

    parsed = _extract_response_fields(raw)
    status = _authorization_status(parsed)
    cpt_codes = [normalize_code(code) for code in parsed.get("cpt_codes", []) if code]
    if status == "approved" and not cpt_codes:
        cpt_codes = required_codes

    return {
        "status": status,
        "transaction_status": parsed.get("transaction_status"),
        "outcome": parsed.get("outcome"),
        "decision": parsed.get("decision"),
        "pre_auth_ref": parsed.get("pre_auth_ref"),
        "payer_id": parsed.get("payer_id") or payer.get("id"),
        "claim_id": parsed.get("claim_id") or claim.get("claim_id"),
        "cpt_codes": cpt_codes,
        "valid_from": parsed.get("valid_from"),
        "valid_to": parsed.get("valid_to"),
        "message": parsed.get("message"),
        "raw_response": raw,
    }


def _coerce_response(raw_response: Any) -> dict[str, Any]:
    if raw_response is None:
        return {}
    if isinstance(raw_response, dict):
        return raw_response
    if isinstance(raw_response, str):
        text = raw_response.strip()
        if not text:
            return {}
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {"items": parsed}
            except json.JSONDecodeError:
                return {"payload": text}
        return {"payload": text}
    return {"payload": raw_response}


def _extract_response_fields(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload") or raw.get("body") or raw.get("response")
    if isinstance(payload, dict):
        raw = {**raw, **payload}
    elif isinstance(payload, str) and payload.strip().startswith("<"):
        return _extract_xml_response_fields(payload, raw)

    bundle = raw if raw.get("resourceType") == "Bundle" else None
    if not bundle and isinstance(payload, str) and payload.strip().startswith("{"):
        try:
            candidate = json.loads(payload)
            if isinstance(candidate, dict) and candidate.get("resourceType") == "Bundle":
                bundle = candidate
        except json.JSONDecodeError:
            bundle = None
    if bundle:
        return _extract_fhir_response_fields(bundle, raw)

    return _extract_flat_response_fields(raw)


def _extract_flat_response_fields(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_status": _first_value(raw, "transaction_status", "outcome", "status"),
        "outcome": _first_value(raw, "outcome"),
        "decision": _first_value(raw, "decision", "authorization_status", "auth_status", "approval_status", "result"),
        "pre_auth_ref": _first_value(
            raw,
            "pre_auth_ref",
            "preAuthRef",
            "prior_authorization_id",
            "priorAuthorizationId",
            "authorization_id",
            "authorizationId",
            "authorization_number",
            "authorizationNumber",
            "reference",
        ),
        "payer_id": _first_value(raw, "payer_id", "payerId", "payer"),
        "claim_id": _first_value(raw, "claim_id", "claimId"),
        "cpt_codes": _codes_from(raw),
        "valid_from": _first_value(raw, "valid_from", "validFrom", "start", "startDate"),
        "valid_to": _first_value(raw, "valid_to", "validTo", "end", "endDate", "expires", "expiryDate"),
        "message": _first_value(raw, "message", "disposition", "error", "reason"),
    }


def _extract_fhir_response_fields(bundle: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    resources = [
        entry.get("resource", {})
        for entry in bundle.get("entry", [])
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    ]
    claim_response = next(
        (
            resource
            for resource in resources
            if resource.get("resourceType") in {"ClaimResponse", "AuthorizationResponse"}
        ),
        {},
    )
    if not claim_response:
        claim_response = next((resource for resource in resources if resource.get("preAuthRef")), {})

    pre_auth_ref = claim_response.get("preAuthRef")
    if isinstance(pre_auth_ref, list):
        pre_auth_ref = pre_auth_ref[0] if pre_auth_ref else None

    return {
        **_extract_flat_response_fields(raw),
        "transaction_status": claim_response.get("outcome") or claim_response.get("status") or raw.get("status"),
        "outcome": claim_response.get("outcome") or raw.get("outcome"),
        "decision": _first_value(claim_response, "decision", "authorizationStatus", "status") or raw.get("decision"),
        "pre_auth_ref": pre_auth_ref or _extract_flat_response_fields(raw).get("pre_auth_ref"),
        "payer_id": _identifier_value(claim_response.get("insurer")) or _extract_flat_response_fields(raw).get("payer_id"),
        "claim_id": _reference_id(claim_response.get("request", {}).get("reference")) or _extract_flat_response_fields(raw).get("claim_id"),
        "cpt_codes": _codes_from_fhir_claim_response(claim_response) or _extract_flat_response_fields(raw).get("cpt_codes", []),
        "valid_from": _first_value(claim_response, "validFrom", "valid_from") or _extract_flat_response_fields(raw).get("valid_from"),
        "valid_to": _first_value(claim_response, "validTo", "valid_to", "expirationDate") or _extract_flat_response_fields(raw).get("valid_to"),
        "message": claim_response.get("disposition") or _extract_flat_response_fields(raw).get("message"),
    }


def _extract_xml_response_fields(payload: str, raw: dict[str, Any]) -> dict[str, Any]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return _extract_flat_response_fields(raw)

    values: dict[str, list[str]] = {}
    for element in root.iter():
        key = _local_name(element.tag).lower()
        text = (element.text or "").strip()
        if text:
            values.setdefault(key, []).append(text)

    def xml_first(*names: str) -> str | None:
        for name in names:
            items = values.get(name.lower())
            if items:
                return items[0]
        return None

    codes = []
    for key in ("code", "cpt", "procedurecode", "activitycode"):
        codes.extend(values.get(key, []))

    return {
        **_extract_flat_response_fields(raw),
        "transaction_status": xml_first("status", "outcome", "result"),
        "outcome": xml_first("outcome"),
        "decision": xml_first("decision", "authorizationstatus", "approvalstatus", "result"),
        "pre_auth_ref": xml_first(
            "preauthref",
            "priorauthorizationid",
            "idpayer",
            "authorizationid",
            "authorizationnumber",
            "preauthorizationno",
            "preauthid",
        ),
        "payer_id": xml_first("payerid", "receiverid"),
        "claim_id": xml_first("claimid", "id"),
        "cpt_codes": codes,
        "valid_from": xml_first("validfrom", "start", "fromdate"),
        "valid_to": xml_first("validto", "end", "todate", "expirydate"),
        "message": xml_first("message", "denialreason", "comment", "disposition"),
    }


def _authorization_status(parsed: dict[str, Any]) -> str:
    values = [
        str(parsed.get("decision") or "").strip().lower(),
        str(parsed.get("outcome") or "").strip().lower(),
        str(parsed.get("transaction_status") or "").strip().lower(),
    ]
    if any(value in WAITING_RESPONSE_STATUSES for value in values):
        return "waiting"
    if any(value in ERROR_RESPONSE_STATUSES for value in values):
        return "error"
    if any(value in DENIED_RESPONSE_STATUSES for value in values):
        return "denied"
    if any(value in APPROVED_RESPONSE_STATUSES for value in values):
        return "approved"
    if parsed.get("pre_auth_ref") and any(value in REFERENCE_ACCEPTED_RESPONSE_STATUSES for value in values):
        return "approved"
    if parsed.get("pre_auth_ref") and any(value in COMPLETE_RESPONSE_STATUSES for value in values):
        return "approved"
    if parsed.get("pre_auth_ref") and not any(values):
        return "approved"
    if any(value in COMPLETE_RESPONSE_STATUSES for value in values):
        return "denied"
    return "unknown"


def _codes_from(raw: dict[str, Any]) -> list[str]:
    candidates = []
    for key in ("cpt_codes", "procedure_codes", "service_codes", "approved_codes", "codes"):
        value = raw.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif value:
            candidates.append(value)
    return [str(code) for code in candidates if code]


def _codes_from_fhir_claim_response(claim_response: dict[str, Any]) -> list[str]:
    codes = []
    for item in claim_response.get("item", []):
        service = item.get("productOrService", {})
        for coding in service.get("coding", []):
            if coding.get("code"):
                codes.append(coding["code"])
    return codes


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _identifier_value(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    identifier = value.get("identifier")
    if isinstance(identifier, dict):
        return identifier.get("value")
    return None


def _reference_id(reference: str | None) -> str | None:
    if not reference:
        return None
    return str(reference).split("/")[-1]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _callback_response(state: dict[str, Any]) -> Any:
    callback_results = state.get("callback_results", {})
    if isinstance(callback_results, dict):
        for key in ("parse_final_response", "prior_auth_response", "pa_response"):
            if callback_results.get(key):
                return callback_results[key]
    return state.get("prior_auth_response") or state.get("payer_response")


def _payload_contains_pre_auth_ref(state: dict[str, Any], pre_auth_ref: str | None) -> bool:
    if not pre_auth_ref:
        return False
    payload = state.get("claim_payload") or ""
    return str(pre_auth_ref) in str(payload)


def _prior_auth_request_id(state: dict[str, Any], normalized: dict[str, Any]) -> str | None:
    raw = normalized.get("raw_response") or {}
    if isinstance(raw, dict):
        for key in ("request_id", "prior_auth_request_id", "authorization_request_id"):
            if raw.get(key):
                return raw[key]
    existing = state.get("prior_auth_existing_request") or {}
    return state.get("prior_auth_request_id") or existing.get("request_id")


def build_prior_auth_subgraph(
    *,
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
    repository: RepositoryInterface,
    pa_builder: PAClaimBuilderModule,
    object_store: ObjectStoreInterface | None = None,
):
    """Reusable Prior Auth Check State Machine from the MD."""

    def normalize_prior_auth_input(state: dict[str, Any]) -> dict[str, Any]:
        claim = state.get("canonical_claim", {})
        return {
            **state,
            "prior_auth_input": {
                "activities": [
                    {
                        "code": line.get("code"),
                        "service_date": claim.get("encounter", {}).get("service_date"),
                        "payer_id": claim.get("payer", {}).get("id"),
                        "plan_id": claim.get("payer", {}).get("plan_id"),
                    }
                    for line in claim.get("line_items", [])
                    if line.get("code")
                ]
            },
        }

    def determine_requirement(state: dict[str, Any]) -> dict[str, Any]:
        claim = state.get("canonical_claim", {})
        payer = claim.get("payer", {})
        required_codes = [
            activity["code"]
            for activity in state.get("prior_auth_input", {}).get("activities", [])
            if pa_required_for_code(
                payer_id=payer.get("id", ""),
                plan_id=payer.get("plan_id", ""),
                cpt_code=activity["code"],
                payer_rules=payer_rules,
                kg_client=kg_client,
            )
        ]
        if not required_codes:
            result = CheckResult("PRIOR_AUTH", PriorAuthStatus.NOT_REQUIRED, data={"required_codes": []})
            return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result, "prior_auth_terminal": True}
        return {**state, "prior_auth_required_codes": required_codes, "prior_auth_terminal": False}

    def validate_existing_auth(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal"):
            return state
        claim = state.get("canonical_claim", {})
        payer = claim.get("payer", {})
        valid_refs: list[str] = []
        issues: list[CheckIssue] = []
        missing_codes: list[str] = []
        for code in state.get("prior_auth_required_codes", []):
            response = repository.find_prior_auth_response(claim["claim_id"], payer.get("id"), code)
            if response and auth_valid(response, code, claim.get("encounter", {}).get("service_date")):
                valid_refs.append(response.get("pre_auth_ref"))
            elif response:
                issues.append(
                    CheckIssue(
                        code="PA_EXPIRED_OR_DENIED",
                        severity=Severity.CRITICAL,
                        check_type="PRIOR_AUTH",
                        field="prior_auth_response",
                        message=f"Prior authorization for {code} exists but is expired, denied, or outside service date.",
                        suggestion="Obtain a fresh authorization before submission.",
                        penalty=100,
                    )
                )
            else:
                missing_codes.append(code)
        if issues:
            result = CheckResult("PRIOR_AUTH", PriorAuthStatus.HOLD_CRITICAL, issues)
            return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result, "prior_auth_terminal": True}
        if valid_refs and not missing_codes:
            rebuild_required = not _payload_contains_pre_auth_ref(state, valid_refs[0])
            result = CheckResult(
                "PRIOR_AUTH",
                PriorAuthStatus.ALREADY_VALID,
                data={"valid_refs": valid_refs, "payload_rebuild_required": rebuild_required},
            )
            return {
                **state,
                "canonical_claim": {**claim, "pre_auth_ref": valid_refs[0]},
                "payload_rebuild_required": rebuild_required,
                "prior_auth_result": result.to_dict(),
                "_prior_auth_result_object": result,
                "prior_auth_terminal": True,
            }
        return {**state, "prior_auth_missing_codes": missing_codes, "prior_auth_valid_refs": valid_refs}

    def pa_route_decision(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal"):
            return state
        existing = None
        if hasattr(repository, "prior_auth_requests"):
            for request in getattr(repository, "prior_auth_requests", {}).values():
                if request.get("claim_id") == state.get("canonical_claim", {}).get("claim_id") and request.get("required_codes") == state.get("prior_auth_missing_codes"):
                    existing = request
                    break
        return {**state, "prior_auth_existing_request": existing}

    def pa_claim_builder(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal") or state.get("prior_auth_existing_request"):
            return state
        return pa_builder.build(state, state.get("prior_auth_missing_codes", []))

    def submit_or_create_task(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal"):
            return state
        if state.get("prior_auth_existing_request"):
            return {**state, "prior_auth_request_id": state["prior_auth_existing_request"].get("request_id")}
        claim = state.get("canonical_claim", {})
        request_id = repository.insert_prior_auth_request(
            claim["claim_id"],
            {
                "standard": state.get("route", {}).get("prior_auth_standard"),
                "object_uri": state.get("pa_payload_uri"),
                "status": PriorAuthStatus.REQUIRED_MISSING,
                "required_codes": state.get("prior_auth_missing_codes", []),
            },
        )
        return {**state, "prior_auth_request_id": request_id, "prior_auth_submit_status": "MANUAL_PORTAL_TASK"}

    def poll_if_needed(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def parse_final_response(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_terminal") or state.get("_prior_auth_result_object"):
            return state
        raw_response = _callback_response(state)
        if not raw_response:
            return state

        claim = state.get("canonical_claim", {})
        normalized = normalize_prior_auth_response(raw_response, state)
        request_id = _prior_auth_request_id(state, normalized)
        status = normalized["status"]

        if status == "waiting":
            result = CheckResult(
                "PRIOR_AUTH",
                PriorAuthStatus.WAITING_FOR_PAYER,
                data={"response": normalized, "request_id": request_id},
            )
            return {
                **state,
                "prior_auth_response": normalized,
                "prior_auth_result": result.to_dict(),
                "_prior_auth_result_object": result,
                "payload_status": PayloadStatus.WAITING_FOR_PAYER,
                "prior_auth_terminal": True,
            }

        if request_id and status in {"approved", "denied", "error"}:
            repository.insert_prior_auth_response(request_id, normalized)

        if status == "approved":
            pre_auth_ref = normalized.get("pre_auth_ref")
            if not pre_auth_ref:
                issue = CheckIssue(
                    code="PA_APPROVED_WITHOUT_REFERENCE",
                    severity=Severity.CRITICAL,
                    check_type="PRIOR_AUTH",
                    field="prior_auth_response.pre_auth_ref",
                    message="Payer returned an approved prior authorization response without an authorization reference.",
                    suggestion="Do not submit until the payer authorization reference is available.",
                    penalty=100,
                    evidence={"request_id": request_id, "response": normalized},
                )
                result = CheckResult("PRIOR_AUTH", PriorAuthStatus.HOLD_CRITICAL, [issue], {"response": normalized})
                return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result, "prior_auth_terminal": True}

            cpt_codes = normalized.get("cpt_codes") or state.get("prior_auth_required_codes", [])
            invalid_codes = [
                code
                for code in cpt_codes
                if not auth_valid(normalized, code, claim.get("encounter", {}).get("service_date"))
            ]
            if invalid_codes:
                issue = CheckIssue(
                    code="PA_RESPONSE_NOT_VALID_FOR_SERVICE",
                    severity=Severity.CRITICAL,
                    check_type="PRIOR_AUTH",
                    field="prior_auth_response",
                    message="Approved prior authorization response does not cover the claim service date or requested code.",
                    suggestion="Confirm the payer authorization details before rebuilding the claim.",
                    penalty=100,
                    evidence={"invalid_codes": invalid_codes, "response": normalized},
                )
                result = CheckResult("PRIOR_AUTH", PriorAuthStatus.HOLD_CRITICAL, [issue], {"response": normalized})
                return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result, "prior_auth_terminal": True}

            rebuild_required = not _payload_contains_pre_auth_ref(state, pre_auth_ref)
            result = CheckResult(
                "PRIOR_AUTH",
                PriorAuthStatus.APPROVED,
                data={
                    "valid_refs": [pre_auth_ref],
                    "response": normalized,
                    "request_id": request_id,
                    "payload_rebuild_required": rebuild_required,
                },
            )
            return {
                **state,
                "canonical_claim": {**claim, "pre_auth_ref": pre_auth_ref},
                "prior_auth_response": normalized,
                "prior_auth_valid_refs": [pre_auth_ref],
                "payload_rebuild_required": rebuild_required,
                "prior_auth_result": result.to_dict(),
                "_prior_auth_result_object": result,
                "prior_auth_terminal": True,
            }

        severity = Severity.CRITICAL if status == "error" else Severity.ERROR
        result_status = PriorAuthStatus.HOLD_CRITICAL if status == "error" else PriorAuthStatus.DENIED_NEEDS_REVIEW
        issue = CheckIssue(
            code="PA_RESPONSE_NOT_APPROVED",
            severity=severity,
            check_type="PRIOR_AUTH",
            field="prior_auth_response.status",
            message="Prior authorization response was not approved.",
            suggestion="Route to RCM review, correct the request, or use the payer portal escalation path.",
            penalty=100 if severity == Severity.CRITICAL else 30,
            evidence={"request_id": request_id, "response": normalized},
        )
        result = CheckResult("PRIOR_AUTH", result_status, [issue], {"response": normalized, "request_id": request_id})
        return {**state, "prior_auth_response": normalized, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result, "prior_auth_terminal": True}

    def patch_claim(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("prior_auth_valid_refs"):
            return {
                **state,
                "canonical_claim": {**state.get("canonical_claim", {}), "pre_auth_ref": state["prior_auth_valid_refs"][0]},
                "payload_rebuild_required": True,
            }
        return state

    def finish(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("_prior_auth_result_object"):
            return state
        missing_codes = state.get("prior_auth_missing_codes", [])
        issues = []
        if missing_codes:
            issues.append(
                CheckIssue(
                    code="PA_REQUIRED_MISSING",
                    severity=Severity.ERROR,
                    check_type="PRIOR_AUTH",
                    field="canonical_claim.procedures",
                    message=f"Prior authorization is required for {', '.join(missing_codes)}.",
                    suggestion="Submit the generated PA payload and wait for approval.",
                    penalty=20,
                    evidence={"request_id": state.get("prior_auth_request_id"), "pa_payload_uri": state.get("pa_payload_uri")},
                )
            )
        result = CheckResult(
            "PRIOR_AUTH",
            PriorAuthStatus.REQUIRED_MISSING if issues else PriorAuthStatus.ALREADY_VALID,
            issues,
            {"required_codes": state.get("prior_auth_required_codes", []), "valid_refs": state.get("prior_auth_valid_refs", [])},
        )
        return {**state, "prior_auth_result": result.to_dict(), "_prior_auth_result_object": result}

    graph = StateGraph(dict)
    nodes = {
        "normalize_prior_auth_input": normalize_prior_auth_input,
        "determine_requirement": determine_requirement,
        "validate_existing_auth": validate_existing_auth,
        "pa_route_decision": pa_route_decision,
        "pa_claim_builder": pa_claim_builder,
        "submit_or_create_task": submit_or_create_task,
        "poll_if_needed": poll_if_needed,
        "parse_final_response": parse_final_response,
        "patch_claim": patch_claim,
        "finish": finish,
    }
    for name, fn in nodes.items():
        graph.add_node(
            name,
            audited_node(
                agent="PriorAuthCheckSubgraph",
                node=name,
                fn=fn,
                repository=repository,
                object_store=object_store,
            ),
        )
    graph.add_edge(START, "normalize_prior_auth_input")
    graph.add_edge("normalize_prior_auth_input", "determine_requirement")
    graph.add_edge("determine_requirement", "validate_existing_auth")
    graph.add_edge("validate_existing_auth", "pa_route_decision")
    graph.add_edge("pa_route_decision", "pa_claim_builder")
    graph.add_edge("pa_claim_builder", "submit_or_create_task")
    graph.add_edge("submit_or_create_task", "poll_if_needed")
    graph.add_edge("poll_if_needed", "parse_final_response")
    graph.add_edge("parse_final_response", "patch_claim")
    graph.add_edge("patch_claim", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


def run_prior_auth_subgraph(
    *,
    state: dict[str, Any],
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
    repository: RepositoryInterface,
    pa_builder: PAClaimBuilderModule,
    object_store: ObjectStoreInterface | None = None,
) -> tuple[dict[str, Any], CheckResult]:
    result_state = build_prior_auth_subgraph(
        payer_rules=payer_rules,
        kg_client=kg_client,
        repository=repository,
        pa_builder=pa_builder,
        object_store=object_store,
    ).invoke(state)
    return result_state, result_state["_prior_auth_result_object"]
