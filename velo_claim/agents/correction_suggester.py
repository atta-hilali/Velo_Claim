from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from velo_claim.agents.audit import audited_node, record_audit_event
from velo_claim.core.container import ServiceContainer, build_default_container
from velo_claim.core.enums import (
    AuditEventType,
    CorrectionSource,
    CorrectionStatus,
    CorrectionSuggestionStatus,
    PayloadStatus,
)
from velo_claim.core.models import CorrectionSuggestion, RoutingContext
from velo_claim.core.utils import sha256_text
from velo_claim.corrections.resolvers import (
    CorrectionLLMClient,
    deterministic_candidate,
    kg_evidence_for_group,
    llm_candidate,
    manual_candidate,
    merge_candidates,
)
from velo_claim.corrections.triage import group_issues


AGENT_NAME = "CorrectionSuggesterAgent"
MAX_CORRECTION_CYCLES = 3


class CorrectionContextError(ValueError):
    pass


def build_correction_suggester_agent(
    *,
    container: ServiceContainer | None = None,
    llm_client: CorrectionLLMClient | None = None,
):
    services = container or build_default_container()
    correction_llm = llm_client or CorrectionLLMClient.from_env()

    def load_correction_context(state: dict[str, Any]) -> dict[str, Any]:
        claim_id = state.get("claim_id") or state.get("claim", {}).get("claim_id")
        if not claim_id:
            raise CorrectionContextError("Correction generation requires claim_id.")
        detail = services.repository.get_claim_detail(claim_id)
        if not detail:
            raise CorrectionContextError(f"Claim not found: {claim_id}")
        report_id = state.get("validation_report_id")
        report_row = (
            services.repository.get_validation_report(str(report_id))
            if report_id
            else services.repository.get_latest_validation_report(claim_id)
        )
        if not report_row:
            raise CorrectionContextError("A persisted validation report is required.")
        report_id = str(report_row.get("report_id") or report_row.get("id"))
        final_status = str(report_row.get("final_status") or report_row.get("report", {}).get("status") or "")
        if final_status != "NEEDS_REVIEW":
            return {
                **state,
                "claim_id": claim_id,
                "validation_report_id": report_id,
                "final_status": final_status,
                "correction_eligible": False,
                "correction_status": CorrectionStatus.NOT_STARTED,
                "correction_skip_reason": f"Correction does not handle {final_status or 'UNKNOWN'}.",
            }

        current_version = services.repository.get_current_claim_version(claim_id)
        if not current_version:
            raise CorrectionContextError("The claim has no persisted canonical version.")
        base_version = int(current_version.get("version") or 0)
        report_version = int(report_row.get("version") or 0)
        if report_row.get("version") is not None and report_version != base_version:
            raise CorrectionContextError(
                f"Validation report version {report_version} is stale for current claim version {base_version}."
            )
        payload_row = detail.get("claim_payload") or services.repository.latest_claim_payload(claim_id) or {}
        issues = services.repository.list_validation_issues(report_id)
        if not issues:
            raise CorrectionContextError("The NEEDS_REVIEW report has no persisted validation issues.")

        cycles = services.repository.list_correction_cycles(claim_id)
        existing = next(
            (
                cycle
                for cycle in reversed(cycles)
                if str(cycle.get("validation_report_id")) == report_id
                and str(cycle.get("status")) not in {"REJECTED", "EXHAUSTED", "STALE"}
            ),
            None,
        )
        force_new = bool(state.get("force_new_correction_cycle"))
        if existing and not force_new:
            cycle_number = int(existing.get("cycle_number") or 1)
        else:
            cycle_number = max((int(item.get("cycle_number") or 0) for item in cycles), default=0) + 1
        if cycle_number > MAX_CORRECTION_CYCLES:
            services.repository.update_claim_status(
                claim_id,
                str(PayloadStatus.HOLD_CRITICAL),
                {"reason": "CORRECTION_CYCLE_EXHAUSTED", "validation_report_id": report_id},
            )
            _audit(
                services,
                claim_id,
                "load_correction_context",
                AuditEventType.CORRECTION_CYCLE_EXHAUSTED,
                {"validation_report_id": report_id, "attempted_cycle": cycle_number},
            )
            return {
                **state,
                "claim_id": claim_id,
                "correction_eligible": False,
                "payload_status": PayloadStatus.HOLD_CRITICAL,
                "correction_status": CorrectionStatus.EXHAUSTED,
            }

        routing_context = detail.get("routing_context") or current_version.get("routing_context") or state.get("routing_context") or {}
        routing = RoutingContext(**routing_context)
        prior_report_cycle = next(
            (
                cycle
                for cycle in reversed(cycles)
                if str(cycle.get("validation_report_id")) == report_id
                and isinstance(cycle.get("metadata"), dict)
                and cycle.get("metadata", {}).get("payer_rule_set")
            ),
            None,
        )
        payer_rule_set = state.get("payer_rule_set") or (
            prior_report_cycle.get("metadata", {}).get("payer_rule_set")
            if prior_report_cycle
            else None
        )
        if not payer_rule_set:
            payer_rule_set = services.payer_rule_loader.load(routing.payer_id, routing.plan_id).to_dict()
        kg_diagnostics = services.kg_client.diagnostics()
        if (
            os.getenv("VELO_CLAIM_STORAGE", "memory").lower() == "production"
            and str(kg_diagnostics.get("backend") or "").lower() == "mock"
        ):
            raise CorrectionContextError("Production correction workflow cannot use a mock knowledge graph.")
        return {
            **state,
            "claim_id": claim_id,
            "claim": {"claim_id": claim_id, "version": base_version},
            "canonical_claim": current_version.get("canonical_claim") or {},
            "source_context": current_version.get("source_context") or detail.get("source_context") or {},
            "routing_context": routing_context,
            "route": current_version.get("route") or detail.get("route") or {},
            "claim_payload_uri": payload_row.get("object_uri"),
            "claim_payload_type": payload_row.get("payload_type"),
            "payload_version": int(payload_row.get("version") or base_version),
            "validation_report_id": report_id,
            "validation_report": report_row.get("report") or {},
            "validation_issues": issues,
            "payer_rule_set": payer_rule_set,
            "payer_rule_source_version": payer_rule_set.get("source_version"),
            "kg_diagnostics": kg_diagnostics,
            "correction_eligible": True,
            "correction_cycle_id": str(existing.get("cycle_id") or existing.get("id")) if existing and not force_new else None,
            "correction_existing": bool(
                existing
                and not force_new
                and services.repository.list_correction_suggestions(
                    str(existing.get("cycle_id") or existing.get("id"))
                )
            ),
            "correction_cycle_count": cycle_number,
            "correction_status": CorrectionStatus.GENERATING,
            "errors": list(state.get("errors", [])),
            "warnings": list(state.get("warnings", [])),
        }

    def triage_issues(state: dict[str, Any]) -> dict[str, Any]:
        claim_id = state["claim_id"]
        normalized, groups = group_issues(
            state.get("validation_issues", []),
            state.get("canonical_claim", {}),
            lambda path: services.repository.list_correction_history(claim_id, path),
        )
        return {**state, "correction_candidate_issues": normalized, "correction_issue_groups": groups}

    def deterministic_resolver(state: dict[str, Any]) -> dict[str, Any]:
        suggestions = []
        unresolved = []
        for group in state.get("correction_issue_groups", []):
            if group.get("manual_reason"):
                suggestions.append(manual_candidate(group, state["canonical_claim"]))
                continue
            candidate = deterministic_candidate(
                group=group,
                canonical_claim=state["canonical_claim"],
                state=state,
                repository=services.repository,
            )
            if candidate:
                suggestions.append(candidate)
            else:
                unresolved.append(group)
        return {
            **state,
            "deterministic_suggestions": suggestions,
            "unresolved_correction_issues": unresolved,
        }

    def llm_resolver(state: dict[str, Any]) -> dict[str, Any]:
        suggestions = []
        for group in state.get("unresolved_correction_issues", []):
            kg_evidence = kg_evidence_for_group(group, state["canonical_claim"], services.kg_client)
            candidate = llm_candidate(
                group=group,
                canonical_claim=state["canonical_claim"],
                source_context=state.get("source_context", {}),
                route=state.get("route", {}),
                payer_rule_set=state.get("payer_rule_set", {}),
                kg_evidence=kg_evidence,
                client=correction_llm,
            )
            if not candidate:
                reason = (
                    "MedGemma did not return a sufficiently supported correction; human reconciliation is required."
                    if correction_llm.enabled
                    else "Correction LLM is disabled; human reconciliation is required."
                )
                candidate = manual_candidate(group, state["canonical_claim"], reason)
                candidate["evidence"]["kg"] = kg_evidence
                candidate["evidence"]["llm"] = {
                    "enabled": correction_llm.enabled,
                    "configured": bool(correction_llm.base_url),
                    "model": correction_llm.model,
                }
            suggestions.append(candidate)
        return {**state, "llm_suggestions": suggestions}

    def conflict_resolution(state: dict[str, Any]) -> dict[str, Any]:
        candidates = [
            *state.get("deterministic_suggestions", []),
            *state.get("llm_suggestions", []),
        ]
        resolved = merge_candidates(candidates, state["canonical_claim"])
        return {**state, "resolved_correction_candidates": resolved}

    def assemble_suggestions(state: dict[str, Any]) -> dict[str, Any]:
        cycle_number = int(state["correction_cycle_count"])
        assembled = []
        for candidate in state.get("resolved_correction_candidates", []):
            candidate = _respect_rejection_history(
                candidate,
                state.get("correction_issue_groups", []),
                state.get("canonical_claim", {}),
            )
            suggestion_hash = _suggestion_hash(
                claim_id=state["claim_id"],
                base_version=int(state["claim"]["version"]),
                report_id=state["validation_report_id"],
                cycle_number=cycle_number,
                candidate=candidate,
            )
            status = (
                CorrectionSuggestionStatus.MANUAL_RECONCILIATION_REQUIRED
                if candidate["source"] == CorrectionSource.MANUAL_REQUIRED
                else CorrectionSuggestionStatus.PENDING_REVIEW
            )
            suggestion = CorrectionSuggestion(
                suggestion_id=str(uuid4()),
                cycle_id=state.get("correction_cycle_id") or "PENDING_CYCLE",
                claim_id=state["claim_id"],
                base_claim_version=int(state["claim"]["version"]),
                base_payload_version=int(state["payload_version"]),
                validation_report_id=state["validation_report_id"],
                issue_ids=list(candidate.get("issue_ids", [])),
                issue_codes=list(candidate.get("issue_codes", [])),
                field_path=candidate["field_path"],
                old_value=candidate.get("old_value"),
                proposed_value=candidate.get("proposed_value"),
                source=CorrectionSource(candidate["source"]),
                confidence=float(candidate.get("confidence") or 0),
                rationale=candidate["rationale"],
                evidence=candidate.get("evidence") or {},
                rule_refs=candidate.get("rule_refs") or [],
                status=status,
                cycle_count=cycle_number,
                suggestion_hash=suggestion_hash,
            )
            assembled.append(suggestion.to_dict())
        return {**state, "correction_suggestions": assembled}

    def persist_suggestions(state: dict[str, Any]) -> dict[str, Any]:
        cycle = services.repository.create_correction_cycle(
            state["claim_id"],
            {
                "cycle_id": state.get("correction_cycle_id") or str(uuid4()),
                "validation_report_id": state["validation_report_id"],
                "base_claim_version": int(state["claim"]["version"]),
                "base_payload_version": int(state["payload_version"]),
                "cycle_number": int(state["correction_cycle_count"]),
                "status": CorrectionStatus.GENERATING,
                "payer_rule_source_version": state.get("payer_rule_source_version"),
                "metadata": {
                    "kg": state.get("kg_diagnostics", {}),
                    "llm_model": correction_llm.model,
                    "payer_rule_set": state.get("payer_rule_set", {}),
                },
            },
        )
        cycle_id = str(cycle.get("cycle_id") or cycle.get("id"))
        _audit(
            services,
            state["claim_id"],
            "persist_suggestions",
            AuditEventType.CORRECTION_CYCLE_CREATED,
            {"cycle_id": cycle_id, "validation_report_id": state["validation_report_id"], "cycle_number": state["correction_cycle_count"]},
        )
        persisted = []
        for suggestion in state.get("correction_suggestions", []):
            row = services.repository.insert_correction_suggestion({**suggestion, "cycle_id": cycle_id})
            persisted.append(row)
            event_type = (
                AuditEventType.CORRECTION_MANUAL_RECONCILIATION_REQUIRED
                if str(row.get("status")) == str(CorrectionSuggestionStatus.MANUAL_RECONCILIATION_REQUIRED)
                else AuditEventType.CORRECTION_SUGGESTION_CREATED
            )
            _audit(
                services,
                state["claim_id"],
                "persist_suggestions",
                event_type,
                {
                    "cycle_id": cycle_id,
                    "suggestion_id": str(row.get("suggestion_id") or row.get("id")),
                    "field_path": row.get("field_path"),
                    "source": str(row.get("source")),
                    "confidence": float(row.get("confidence") or 0),
                    "rule_refs": row.get("rule_refs") or [],
                },
            )
        return {**state, "correction_cycle_id": cycle_id, "correction_cycle": cycle, "correction_suggestions": persisted}

    def mark_awaiting_human_review(state: dict[str, Any]) -> dict[str, Any]:
        services.repository.update_correction_cycle_status(
            state["correction_cycle_id"], str(CorrectionStatus.AWAITING_HUMAN_REVIEW)
        )
        return {
            **state,
            "payload_status": PayloadStatus.NEEDS_REVIEW,
            "correction_status": CorrectionStatus.AWAITING_HUMAN_REVIEW,
            "next_agent": None,
        }

    def route_after_load(state: dict[str, Any]) -> str:
        if not state.get("correction_eligible"):
            return "skip"
        return "reuse" if state.get("correction_existing") else "continue"

    def route_unresolved(state: dict[str, Any]) -> str:
        return "llm" if state.get("unresolved_correction_issues") else "resolved"

    graph = StateGraph(dict)
    graph.add_node("load_correction_context", _audited(services, "load_correction_context", load_correction_context))
    graph.add_node("triage_issues", _audited(services, "triage_issues", triage_issues))
    graph.add_node("deterministic_resolver", _audited(services, "deterministic_resolver", deterministic_resolver))
    graph.add_node("llm_resolver", _audited(services, "llm_resolver", llm_resolver))
    graph.add_node("conflict_resolution", _audited(services, "conflict_resolution", conflict_resolution))
    graph.add_node("assemble_suggestions", _audited(services, "assemble_suggestions", assemble_suggestions))
    graph.add_node("persist_suggestions", _audited(services, "persist_suggestions", persist_suggestions))
    graph.add_node("mark_awaiting_human_review", _audited(services, "mark_awaiting_human_review", mark_awaiting_human_review))
    graph.add_edge(START, "load_correction_context")
    graph.add_conditional_edges(
        "load_correction_context",
        route_after_load,
        {"continue": "triage_issues", "reuse": END, "skip": END},
    )
    graph.add_edge("triage_issues", "deterministic_resolver")
    graph.add_conditional_edges("deterministic_resolver", route_unresolved, {"llm": "llm_resolver", "resolved": "conflict_resolution"})
    graph.add_edge("llm_resolver", "conflict_resolution")
    graph.add_edge("conflict_resolution", "assemble_suggestions")
    graph.add_edge("assemble_suggestions", "persist_suggestions")
    graph.add_edge("persist_suggestions", "mark_awaiting_human_review")
    graph.add_edge("mark_awaiting_human_review", END)
    return graph.compile()


def run_correction_suggester(
    initial_state: dict[str, Any],
    *,
    container: ServiceContainer | None = None,
    llm_client: CorrectionLLMClient | None = None,
) -> dict[str, Any]:
    return build_correction_suggester_agent(container=container, llm_client=llm_client).invoke(initial_state)


def _audited(services: ServiceContainer, node: str, fn):
    return audited_node(
        agent=AGENT_NAME,
        node=node,
        fn=fn,
        repository=services.repository,
        object_store=services.object_store,
    )


def _audit(
    services: ServiceContainer,
    claim_id: str,
    node: str,
    event_type: AuditEventType,
    payload: dict[str, Any],
) -> None:
    record_audit_event(
        repository=services.repository,
        object_store=services.object_store,
        claim_id=claim_id,
        agent=AGENT_NAME,
        node=node,
        event_type=event_type,
        payload=payload,
    )


def _suggestion_hash(
    *, claim_id: str, base_version: int, report_id: str, cycle_number: int, candidate: dict[str, Any]
) -> str:
    stable = {
        "claim_id": claim_id,
        "base_claim_version": base_version,
        "validation_report_id": report_id,
        "cycle_number": cycle_number,
        "issue_ids": sorted(candidate.get("issue_ids", [])),
        "field_path": candidate["field_path"],
        "proposed_value": candidate.get("proposed_value"),
        "rationale": candidate.get("rationale"),
        "evidence": candidate.get("evidence"),
    }
    return sha256_text(json.dumps(stable, sort_keys=True, default=str))


def _respect_rejection_history(
    candidate: dict[str, Any],
    groups: list[dict[str, Any]],
    canonical_claim: dict[str, Any],
) -> dict[str, Any]:
    group = next((item for item in groups if item["field_path"] == candidate["field_path"]), None)
    if not group or candidate.get("source") == "MANUAL_REQUIRED":
        return candidate
    fingerprint = _proposal_fingerprint(candidate)
    for history in group.get("history", []):
        if str(history.get("status")) != "REJECTED":
            continue
        previous = {
            "field_path": history.get("field_path"),
            "proposed_value": history.get("proposed_value"),
            "rationale": history.get("rationale"),
            "evidence": history.get("evidence"),
        }
        if _proposal_fingerprint(previous) == fingerprint:
            return manual_candidate(
                group,
                canonical_claim,
                "An identical evidence/value proposal was rejected previously; a reviewer must reconcile the field.",
            )
    return candidate


def _proposal_fingerprint(candidate: dict[str, Any]) -> str:
    value = {
        "field_path": candidate.get("field_path"),
        "proposed_value": candidate.get("proposed_value"),
        "rationale": candidate.get("rationale"),
        "evidence": candidate.get("evidence"),
    }
    return sha256_text(json.dumps(value, sort_keys=True, default=str))
