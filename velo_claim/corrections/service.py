from __future__ import annotations

from copy import deepcopy
from typing import Any

from velo_claim.agents.audit import record_audit_event
from velo_claim.agents.claim_validation import run_claim_validation
from velo_claim.agents.correction_suggester import run_correction_suggester
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.core.container import ServiceContainer
from velo_claim.core.enums import (
    AuditEventType,
    CorrectionReviewDecision,
    CorrectionStatus,
    CorrectionSuggestionStatus,
    PayloadStatus,
)
from velo_claim.core.utils import sha256_text, utc_now
from velo_claim.corrections.patches import (
    UnsafeCorrectionError,
    apply_correction,
    get_canonical_value,
    validate_correction_path,
    validate_proposed_value,
    values_equal,
)
from velo_claim.storage.interfaces import DuplicateRecordError


class CorrectionNotFoundError(LookupError):
    pass


class InvalidCorrectionStateError(ValueError):
    pass


class StaleCorrectionError(RuntimeError):
    pass


class CorrectionWorkflowService:
    def __init__(self, services: ServiceContainer) -> None:
        self.services = services
        self.repository = services.repository
        self.builder = ClaimBuilderModule(
            repository=services.repository,
            object_store=services.object_store,
            kg_client=services.kg_client,
            payer_rule_loader=services.payer_rule_loader,
        )

    def list_cycles(self, claim_id: str) -> dict[str, Any]:
        self._claim(claim_id)
        cycles = [self._cycle_response(row) for row in self.repository.list_correction_cycles(claim_id)]
        return {"claim_id": claim_id, "cycles": cycles, "count": len(cycles)}

    def get_cycle(self, claim_id: str, cycle_id: str) -> dict[str, Any]:
        self._claim(claim_id)
        cycle = self.repository.get_correction_cycle(cycle_id)
        if not cycle or cycle.get("claim_id") != claim_id:
            raise CorrectionNotFoundError(f"Correction cycle not found: {cycle_id}")
        return self._cycle_response(cycle)

    def generate(self, claim_id: str, *, validation_report_id: str | None = None, force_new: bool = False) -> dict[str, Any]:
        self._claim(claim_id)
        state = run_correction_suggester(
            {
                "claim_id": claim_id,
                "validation_report_id": validation_report_id,
                "force_new_correction_cycle": force_new,
            },
            container=self.services,
        )
        if not state.get("correction_eligible"):
            raise InvalidCorrectionStateError(
                state.get("correction_skip_reason")
                or f"Claim is not eligible for correction generation: {state.get('final_status')}"
            )
        return self.get_cycle(claim_id, state["correction_cycle_id"])

    def review(
        self,
        *,
        claim_id: str,
        suggestion_id: str,
        decision: str,
        reviewer_id: str,
        modified_value: Any = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        suggestion = self.repository.get_correction_suggestion(suggestion_id)
        if not suggestion or suggestion.get("claim_id") != claim_id:
            raise CorrectionNotFoundError(f"Correction suggestion not found: {suggestion_id}")
        try:
            normalized_decision = CorrectionReviewDecision(decision.upper())
        except ValueError as exc:
            raise InvalidCorrectionStateError(f"Unsupported review decision: {decision}") from exc
        current_status = str(suggestion.get("status"))
        if current_status in {"APPLIED", "STALE"}:
            raise InvalidCorrectionStateError(f"Suggestion is already {current_status}.")
        existing_reviews = self.repository.list_correction_reviews(suggestion_id)
        if existing_reviews:
            existing = existing_reviews[-1]
            if (
                str(existing.get("decision")) == str(normalized_decision)
                and existing.get("reviewer_id") == reviewer_id
            ):
                cycle_id = str(suggestion["cycle_id"])
                response = {
                    "review": existing,
                    "cycle": self.get_cycle(claim_id, cycle_id),
                    "idempotent": True,
                }
                if normalized_decision == CorrectionReviewDecision.REJECTED:
                    current_number = int(response["cycle"]["cycle"]["number"])
                    later = [
                        row
                        for row in self.repository.list_correction_cycles(claim_id)
                        if int(row.get("cycle_number") or 0) > current_number
                    ]
                    if later:
                        next_id = str(later[-1].get("cycle_id") or later[-1].get("id"))
                        response["next_cycle"] = self.get_cycle(claim_id, next_id)
                return response
            raise DuplicateRecordError(f"Correction suggestion already reviewed: {suggestion_id}")

        effective_value = suggestion.get("proposed_value")
        if normalized_decision == CorrectionReviewDecision.MODIFIED:
            if modified_value is None:
                raise InvalidCorrectionStateError("MODIFIED review requires modified_value.")
            effective_value = modified_value
        elif normalized_decision == CorrectionReviewDecision.APPROVED and current_status == "MANUAL_RECONCILIATION_REQUIRED":
            raise InvalidCorrectionStateError(
                "A manual-reconciliation suggestion has no proposed value; use MODIFIED with a reviewed value."
            )

        if normalized_decision in {CorrectionReviewDecision.APPROVED, CorrectionReviewDecision.MODIFIED}:
            self._assert_suggestion_fresh(suggestion)
            validate_proposed_value(suggestion["field_path"], suggestion.get("old_value"), effective_value)

        review = self.repository.insert_correction_review(
            {
                "suggestion_id": suggestion_id,
                "decision": normalized_decision,
                "reviewer_id": reviewer_id,
                "modified_value": modified_value,
                "comment": comment,
                "reviewed_at": utc_now(),
            }
        )
        event_type = {
            CorrectionReviewDecision.APPROVED: AuditEventType.CORRECTION_REVIEW_APPROVED,
            CorrectionReviewDecision.MODIFIED: AuditEventType.CORRECTION_REVIEW_MODIFIED,
            CorrectionReviewDecision.REJECTED: AuditEventType.CORRECTION_REVIEW_REJECTED,
        }[normalized_decision]
        self._audit(
            claim_id,
            "review_suggestion",
            event_type,
            {
                "suggestion_id": suggestion_id,
                "cycle_id": str(suggestion.get("cycle_id")),
                "field_path": suggestion.get("field_path"),
                "reviewer_id": reviewer_id,
                "old_value_hash": _value_hash(suggestion.get("old_value")),
                "new_value_hash": _value_hash(effective_value) if effective_value is not None else None,
            },
        )

        cycle_id = str(suggestion["cycle_id"])
        cycle = self.repository.get_correction_cycle(cycle_id)
        if normalized_decision == CorrectionReviewDecision.REJECTED and cycle:
            cycle_number = int(cycle.get("cycle_number") or 1)
            if cycle_number >= 3:
                self.repository.update_correction_cycle_status(cycle_id, str(CorrectionStatus.EXHAUSTED))
                self.repository.update_claim_status(
                    claim_id,
                    str(PayloadStatus.HOLD_CRITICAL),
                    {"reason": "CORRECTION_CYCLE_EXHAUSTED", "cycle_id": cycle_id},
                )
                self._audit(
                    claim_id,
                    "review_suggestion",
                    AuditEventType.CORRECTION_CYCLE_EXHAUSTED,
                    {"cycle_id": cycle_id, "cycle_number": cycle_number, "reviewer_id": reviewer_id},
                )
            else:
                return {
                    "review": review,
                    "rejected_cycle": self.get_cycle(claim_id, cycle_id),
                    "next_cycle": self.generate(
                        claim_id,
                        validation_report_id=str(cycle["validation_report_id"]),
                        force_new=True,
                    ),
                }
        return {"review": review, "cycle": self.get_cycle(claim_id, cycle_id)}

    def apply(self, *, claim_id: str, cycle_id: str, reviewer_id: str) -> dict[str, Any]:
        detail = self._claim(claim_id)
        cycle = self.repository.get_correction_cycle(cycle_id)
        if not cycle or cycle.get("claim_id") != claim_id:
            raise CorrectionNotFoundError(f"Correction cycle not found: {cycle_id}")
        if str(cycle.get("status")) != str(CorrectionStatus.READY_TO_APPLY):
            raise InvalidCorrectionStateError("Every suggestion must be explicitly APPROVED or MODIFIED before apply.")
        suggestions = self.repository.list_correction_suggestions(cycle_id)
        if not suggestions:
            raise InvalidCorrectionStateError("Correction cycle has no suggestions.")

        current_version = self.repository.get_current_claim_version(claim_id)
        if not current_version:
            raise CorrectionNotFoundError(f"Current claim version not found: {claim_id}")
        base_version = int(cycle.get("base_claim_version") or 0)
        if int(current_version.get("version") or 0) != base_version:
            self._mark_cycle_stale(claim_id, cycle_id, suggestions, "Base claim version changed.")
        canonical = deepcopy(current_version.get("canonical_claim") or {})
        for suggestion in suggestions:
            if str(suggestion.get("status")) not in {"APPROVED", "MODIFIED"}:
                raise InvalidCorrectionStateError("Correction cycle contains an unresolved suggestion.")
            self._assert_suggestion_fresh(suggestion, current_version=current_version)
            reviews = self.repository.list_correction_reviews(str(suggestion.get("suggestion_id") or suggestion.get("id")))
            review = reviews[-1] if reviews else None
            if not review:
                raise InvalidCorrectionStateError("Correction suggestion has no persisted review.")
            proposed = (
                review.get("modified_value")
                if str(review.get("decision")) == str(CorrectionReviewDecision.MODIFIED)
                else suggestion.get("proposed_value")
            )
            canonical = apply_correction(
                canonical,
                field_path=suggestion["field_path"],
                expected_old_value=suggestion.get("old_value"),
                proposed_value=proposed,
            )

        new_version = base_version + 1
        build_state = {
            "claim": {"claim_id": claim_id, "version": base_version},
            "canonical_claim": canonical,
            "source_context": current_version.get("source_context") or detail.get("source_context") or {},
            "routing_context": current_version.get("routing_context") or detail.get("routing_context") or {},
            "route": current_version.get("route") or detail.get("route") or {},
            "claim_format": (current_version.get("route") or detail.get("route") or {}).get("claim_standard"),
            "payload_version": base_version,
            "correction_cycle_id": cycle_id,
            "rebuild_reason": "HUMAN_APPROVED_CORRECTION",
            "created_by_agent": "CorrectionSuggesterAgent",
            "errors": [],
            "warnings": [],
        }
        try:
            rebuilt = self.builder.build_payload_from_canonical(
                build_state,
                canonical,
                rebuild_reason="HUMAN_APPROVED_CORRECTION",
                created_by_agent="CorrectionSuggesterAgent",
                persist=False,
            )
            self.repository.commit_corrected_claim(
                claim_id=claim_id,
                cycle_id=cycle_id,
                expected_base_version=base_version,
                new_version=new_version,
                version_data={
                    **rebuilt["_claim_version_data"],
                    "canonical_claim": canonical,
                    "parent_version": base_version,
                    "correction_cycle_id": cycle_id,
                    "reviewer_id": reviewer_id,
                },
                payload_data=rebuilt["_claim_payload_data"],
            )
        except Exception as exc:
            self.repository.update_claim_status(
                claim_id,
                str(PayloadStatus.NEEDS_REVIEW),
                {"reason": "CORRECTION_PAYLOAD_REBUILD_FAILED", "cycle_id": cycle_id},
            )
            self._audit(
                claim_id,
                "apply_cycle",
                AuditEventType.NODE_ERROR,
                {"cycle_id": cycle_id, "base_claim_version": base_version, "error_type": type(exc).__name__},
            )
            raise
        self._audit(
            claim_id,
            "apply_cycle",
            AuditEventType.CORRECTION_CYCLE_APPLIED,
            {"cycle_id": cycle_id, "base_claim_version": base_version, "new_claim_version": new_version, "reviewer_id": reviewer_id},
        )
        self._audit(
            claim_id,
            "apply_cycle",
            AuditEventType.CLAIM_VERSION_CREATED_FROM_CORRECTION,
            {"cycle_id": cycle_id, "parent_version": base_version, "new_claim_version": new_version, "payload_hash": rebuilt["claim_payload_hash"]},
        )

        validation_state = {
            **rebuilt,
            "claim": {"claim_id": claim_id, "version": new_version},
            "canonical_claim": canonical,
            "payload_version": new_version,
            "eligibility_result": detail.get("eligibility_result") or {},
            "prior_auth": detail.get("prior_auth") or {},
            "correction_cycle_count": int(cycle.get("cycle_number") or 1),
        }
        try:
            validated = run_claim_validation(validation_state, container=self.services)
        except Exception as exc:
            self.repository.update_claim_status(
                claim_id,
                str(PayloadStatus.NEEDS_REVIEW),
                {"reason": "CORRECTION_REVALIDATION_FAILED", "cycle_id": cycle_id},
            )
            self._audit(
                claim_id,
                "revalidate_corrected_claim",
                AuditEventType.NODE_ERROR,
                {"cycle_id": cycle_id, "new_claim_version": new_version, "error_type": type(exc).__name__},
            )
            raise
        self._audit(
            claim_id,
            "revalidate_corrected_claim",
            AuditEventType.CORRECTION_REVALIDATION_COMPLETED,
            {
                "cycle_id": cycle_id,
                "new_claim_version": new_version,
                "validation_report_id": validated.get("validation_report_id"),
                "final_status": str(validated.get("final_status")),
            },
        )
        next_cycle = None
        if str(validated.get("final_status")) == "NEEDS_REVIEW":
            if int(cycle.get("cycle_number") or 1) >= 3:
                self.repository.update_claim_status(
                    claim_id,
                    str(PayloadStatus.HOLD_CRITICAL),
                    {"reason": "CORRECTION_CYCLE_EXHAUSTED", "cycle_id": cycle_id},
                )
                self._audit(
                    claim_id,
                    "revalidate_corrected_claim",
                    AuditEventType.CORRECTION_CYCLE_EXHAUSTED,
                    {"cycle_id": cycle_id, "new_claim_version": new_version},
                )
                validated = {
                    **validated,
                    "payload_status": PayloadStatus.HOLD_CRITICAL,
                    "next_agent": None,
                    "correction_status": CorrectionStatus.EXHAUSTED,
                }
            else:
                next_cycle = self.generate(
                    claim_id,
                    validation_report_id=validated.get("validation_report_id"),
                    force_new=True,
                )
        return {
            "claim_id": claim_id,
            "applied_cycle_id": cycle_id,
            "base_claim_version": base_version,
            "new_claim_version": new_version,
            "payload_uri": rebuilt.get("claim_payload_uri"),
            "payload_hash": rebuilt.get("claim_payload_hash"),
            "validation": {
                "validation_report_id": validated.get("validation_report_id"),
                "score": validated.get("score"),
                "final_status": str(validated.get("final_status")),
                "payload_status": str(validated.get("payload_status")),
                "next_agent": validated.get("next_agent"),
            },
            "next_cycle": next_cycle,
        }

    def _claim(self, claim_id: str) -> dict[str, Any]:
        detail = self.repository.get_claim_detail(claim_id)
        if not detail:
            raise CorrectionNotFoundError(f"Claim not found: {claim_id}")
        return detail

    def _assert_suggestion_fresh(
        self, suggestion: dict[str, Any], *, current_version: dict[str, Any] | None = None
    ) -> None:
        claim_id = str(suggestion["claim_id"])
        current_version = current_version or self.repository.get_current_claim_version(claim_id)
        stale_reason = None
        if not current_version or int(current_version.get("version") or 0) != int(suggestion.get("base_claim_version") or 0):
            stale_reason = "Base claim version changed."
        else:
            try:
                actual = get_canonical_value(current_version.get("canonical_claim") or {}, suggestion["field_path"])
            except UnsafeCorrectionError:
                stale_reason = "The target field no longer exists."
            else:
                if not values_equal(actual, suggestion.get("old_value")):
                    stale_reason = "The target field value changed."
        if stale_reason:
            suggestion_id = str(suggestion.get("suggestion_id") or suggestion.get("id"))
            self.repository.update_correction_suggestion_status(suggestion_id, str(CorrectionSuggestionStatus.STALE))
            cycle_id = str(suggestion.get("cycle_id") or "")
            if cycle_id:
                self.repository.update_correction_cycle_status(cycle_id, str(CorrectionStatus.STALE))
            self._audit(
                claim_id,
                "stale_check",
                AuditEventType.CORRECTION_SUGGESTION_STALE,
                {"suggestion_id": suggestion_id, "field_path": suggestion.get("field_path"), "reason": stale_reason},
            )
            raise StaleCorrectionError(stale_reason)

    def _mark_cycle_stale(
        self, claim_id: str, cycle_id: str, suggestions: list[dict[str, Any]], reason: str
    ) -> None:
        for suggestion in suggestions:
            suggestion_id = str(suggestion.get("suggestion_id") or suggestion.get("id"))
            self.repository.update_correction_suggestion_status(suggestion_id, str(CorrectionSuggestionStatus.STALE))
        self.repository.update_correction_cycle_status(cycle_id, str(CorrectionStatus.STALE))
        self._audit(
            claim_id,
            "stale_check",
            AuditEventType.CORRECTION_SUGGESTION_STALE,
            {"cycle_id": cycle_id, "reason": reason},
        )
        raise StaleCorrectionError(reason)

    def _cycle_response(self, cycle: dict[str, Any]) -> dict[str, Any]:
        cycle_id = str(cycle.get("cycle_id") or cycle.get("id"))
        current_version = self.repository.get_current_claim_version(str(cycle.get("claim_id"))) or {}
        canonical_claim = current_version.get("canonical_claim") or {}
        suggestions = []
        for row in self.repository.list_correction_suggestions(cycle_id):
            suggestion_id = str(row.get("suggestion_id") or row.get("id"))
            field_path = str(row.get("field_path") or "")
            try:
                validate_correction_path(field_path)
                get_canonical_value(canonical_claim, field_path)
                can_modify = True
            except UnsafeCorrectionError:
                can_modify = False
            suggestions.append(
                {
                    "id": suggestion_id,
                    "issue_ids": row.get("issue_ids") or [],
                    "issue_codes": row.get("issue_codes") or [],
                    "field_path": field_path,
                    "old_value": row.get("old_value"),
                    "proposed_value": row.get("proposed_value"),
                    "source": str(row.get("source")),
                    "confidence": float(row.get("confidence") or 0),
                    "rationale": row.get("rationale"),
                    "evidence": row.get("evidence") or {},
                    "rule_refs": row.get("rule_refs") or [],
                    "status": str(row.get("status")),
                    "can_modify": can_modify,
                    "reviews": self.repository.list_correction_reviews(suggestion_id),
                }
            )
        return {
            "claim_id": cycle.get("claim_id"),
            "base_claim_version": int(cycle.get("base_claim_version") or 0),
            "base_payload_version": int(cycle.get("base_payload_version") or 0),
            "validation_report_id": str(cycle.get("validation_report_id")),
            "cycle": {
                "id": cycle_id,
                "number": int(cycle.get("cycle_number") or 0),
                "status": str(cycle.get("status")),
                "payer_rule_source_version": cycle.get("payer_rule_source_version"),
                "created_at": cycle.get("created_at"),
                "updated_at": cycle.get("updated_at"),
            },
            "suggestions": suggestions,
        }

    def _audit(self, claim_id: str, node: str, event_type: AuditEventType, payload: dict[str, Any]) -> None:
        record_audit_event(
            repository=self.repository,
            object_store=self.services.object_store,
            claim_id=claim_id,
            agent="CorrectionSuggesterAgent",
            node=node,
            event_type=event_type,
            payload=payload,
        )


def _value_hash(value: Any) -> str:
    import json

    return sha256_text(json.dumps(value, sort_keys=True, default=str))
