from __future__ import annotations

from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from velo_claim.api.app import create_app
from velo_claim.agents.claim_validation import run_claim_validation
from velo_claim.agents.correction_suggester import CorrectionContextError, run_correction_suggester
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.checks.coding import check_coding_consistency
from velo_claim.core.container import build_default_container
from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult
from velo_claim.corrections.patches import UnsafeCorrectionError, apply_correction
from velo_claim.corrections.resolvers import CorrectionLLMClient, CorrectionLLMResponse, merge_candidates
from velo_claim.corrections.service import (
    CorrectionWorkflowService,
    InvalidCorrectionStateError,
    StaleCorrectionError,
)


class DisabledLLM:
    enabled = False
    base_url = ""
    model = "medgemma"

    def resolve(self, context):
        raise AssertionError("Disabled LLM must not be called")


class TrackingLLM:
    enabled = True
    base_url = "http://medgemma.test"
    model = "medgemma"
    minimum_confidence = 0.8

    def __init__(self, response: CorrectionLLMResponse | None = None) -> None:
        self.calls = 0
        self.response = response
        self.contexts = []

    def resolve(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.response is None:
            raise RuntimeError("unavailable")
        return self.response


@pytest.fixture()
def services(monkeypatch):
    monkeypatch.setenv("VELO_CLAIM_STORAGE", "memory")
    return build_default_container()


def _canonical(*, gross: float = 90.0) -> dict:
    return {
        "claim_id": "CLM-CORRECTION-1",
        "patient": {
            "id": "PAT-1",
            "name": "Test Patient",
            "member_id": "MEM-1",
            "national_identifier": "1000000001",
            "birth_date": "1990-01-01",
            "gender": "male",
        },
        "payer": {
            "id": "A001",
            "name": "Test Payer",
            "plan_id": "PLAN-1",
            "coverage_id": "COV-1",
            "coverage_status": "active",
            "coverage_period": {"start": "2026-01-01", "end": "2026-12-31"},
            "eligibility_ref": "ELIG-1",
        },
        "provider": {
            "id": "PRAC-1",
            "name": "Test Clinician",
            "license": "LIC-1",
            "facility_id": "FAC-1",
            "facility_name": "Test Facility",
            "facility_license": "FAC-LIC-1",
        },
        "encounter": {
            "id": "ENC-1",
            "type": "AMB",
            "class_code": "AMB",
            "service_date": "2026-06-01",
            "period": {"start": "2026-06-01T09:00:00Z", "end": "2026-06-01T09:30:00Z"},
        },
        "diagnoses": [{"system": "ICD-10", "code": "I10", "description": "Hypertension", "type": "principal"}],
        "procedures": [{"system": "CPT", "code": "99213", "description": "Office visit", "quantity": 1}],
        "line_items": [
            {
                "id": "ACT-1",
                "system": "CPT",
                "code": "99213",
                "description": "Office visit",
                "quantity": 1,
                "gross": 100.0,
                "net": 100.0,
                "patient_share": 0.0,
                "currency": "SAR",
                "service_date": "2026-06-01",
            }
        ],
        "attachments": [{"type": "SOAP_NOTE", "name": "soap.pdf"}],
        "amount": {"gross": gross, "net": 100.0, "patient_share": 0.0, "currency": "SAR"},
        "pre_auth_ref": "PA-1",
    }


def _seed(
    services,
    *,
    final_status: str = "NEEDS_REVIEW",
    issues: list[dict] | None = None,
    canonical: dict | None = None,
):
    repository = services.repository
    canonical = deepcopy(canonical or _canonical())
    claim_id = canonical["claim_id"]
    route = {
        "jurisdiction": "KSA",
        "claim_standard": "NPHIES",
        "prior_auth_standard": "NPHIES",
        "eligibility_profile": "NPHIES",
        "payer_rule_profile": "DEFAULT",
        "submission_channel": "NPHIES",
    }
    routing_context = {
        "payer_id": "A001",
        "payer_name": "Test Payer",
        "plan_id": "PLAN-1",
        "jurisdiction_hint": "KSA",
        "facility_license": "FAC-LIC-1",
        "provider_license": "LIC-1",
        "currency": "SAR",
    }
    source_context = {
        "patient": {"id": "PAT-1"},
        "coverage": {"id": "COV-1", "status": "active"},
        "encounter": {"id": "ENC-1", "type": "EMER"},
        "provider": {"id": "PRAC-1"},
        "facility": {"id": "FAC-1"},
        "conditions": [],
        "procedures": [],
        "attachments": [],
        "charge_items": [],
        "payer_rules": [],
    }
    repository.upsert_claim(
        claim_id,
        {
            "status": final_status,
            "jurisdiction": "KSA",
            "payer_id": "A001",
            "provider_id": "LIC-1",
            "patient_id": "PAT-1",
        },
    )
    repository.put_route_decision(claim_id, route)
    repository.insert_claim_version(
        claim_id,
        1,
        {
            "canonical_claim": canonical,
            "route": route,
            "routing_context": routing_context,
            "source_context": source_context,
            "created_by_agent": "test",
        },
    )
    uri = services.object_store.put_text("claims/CLM-CORRECTION-1/versions/1/payload.json", "{}", "application/fhir+json")
    repository.insert_claim_payload(
        claim_id,
        1,
        {
            "standard": "NPHIES",
            "payload_type": "fhir_bundle_json",
            "object_uri": uri,
            "sha256_hash": "old-hash",
            "status": "NEEDS_REVIEW",
        },
    )
    report_id = repository.insert_validation_report(
        claim_id,
        {
            "version": 1,
            "score": 75,
            "final_status": final_status,
            "report": {"claim_id": claim_id, "score": 75, "status": final_status},
        },
    )
    default_issues = [
        {
            "check_type": "FINANCIAL",
            "severity": "ERROR",
            "code": "FINANCIAL_GROSS_MISMATCH",
            "field": "canonical_claim.amount.gross",
            "message": "Gross total mismatch.",
            "suggestion": "Recalculate from lines.",
            "penalty": 20,
        }
    ]
    for issue in issues if issues is not None else default_issues:
        repository.insert_validation_issue(report_id, issue)
    return claim_id, report_id


@pytest.mark.parametrize(
    "status",
    ["READY_TO_SUBMIT", "WAITING_FOR_PAYER", "NEEDS_PRIOR_AUTH", "NEEDS_PAYLOAD_REBUILD", "HOLD_CRITICAL"],
)
def test_only_needs_review_enters_correction_graph(services, status) -> None:
    claim_id, report_id = _seed(services, final_status=status)
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=DisabledLLM(),
    )
    assert result["correction_eligible"] is False
    assert services.repository.list_correction_cycles(claim_id) == []


def test_same_field_issues_merge_and_deterministic_skips_llm(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "FINANCIAL",
                "severity": "ERROR",
                "code": code,
                "field": "canonical_claim.amount.gross",
                "message": code,
                "suggestion": "Recalculate.",
            }
            for code in ("FINANCIAL_GROSS_MISMATCH", "ANOTHER_GROSS_CHECK")
        ],
    )
    llm = TrackingLLM()
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    assert llm.calls == 0
    assert len(result["correction_issue_groups"]) == 1
    assert len(result["correction_suggestions"]) == 1
    suggestion = result["correction_suggestions"][0]
    assert suggestion["proposed_value"] == 100.0
    assert suggestion["source"] == "RULE_ENGINE"
    assert suggestion["rule_refs"][0]["approved_by"] == "VELO_CLAIM_CORE"


def test_generation_is_idempotent(services) -> None:
    claim_id, report_id = _seed(services)
    state = {"claim_id": claim_id, "validation_report_id": report_id}
    first = run_correction_suggester(state, container=services, llm_client=DisabledLLM())
    second = run_correction_suggester(state, container=services, llm_client=DisabledLLM())
    assert first["correction_cycle_id"] == second["correction_cycle_id"]
    assert len(services.repository.list_correction_cycles(claim_id)) == 1
    assert len(services.repository.list_correction_suggestions(first["correction_cycle_id"])) == 1


def test_protected_payer_issue_becomes_manual_reconciliation(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "PAYER_MISMATCH",
                "field": "canonical_claim.payer.id",
                "message": "Payer mismatch.",
                "suggestion": "Review payer.",
            }
        ],
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=DisabledLLM(),
    )
    suggestion = result["correction_suggestions"][0]
    assert suggestion["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert suggestion["proposed_value"] is None


def test_unresolved_issue_uses_medgemma_contract(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "ENCOUNTER_TYPE_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Encounter type differs from source.",
                "suggestion": "Use source encounter type.",
            }
        ],
    )
    llm = TrackingLLM(
        CorrectionLLMResponse(
            can_suggest=True,
            field_path="canonical_claim.encounter.type",
            current_value="AMB",
            proposed_value="EMER",
            rationale="The source encounter records EMER.",
            confidence=0.95,
            evidence_refs=["source:source_context.encounter.type"],
            requires_manual_reconciliation=False,
        )
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    assert llm.calls == 1
    assert result["correction_suggestions"][0]["proposed_value"] == "EMER"
    assert result["correction_suggestions"][0]["source"] == "LLM"


def test_llm_failure_degrades_to_manual(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "DOCUMENTATION",
                "severity": "WARNING",
                "code": "DOCUMENTATION_WARNING",
                "field": "canonical_claim.attachments",
                "message": "Review attachments.",
                "suggestion": "Review.",
            }
        ],
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=TrackingLLM(),
    )
    assert result["correction_suggestions"][0]["source"] == "MANUAL_REQUIRED"


def test_conflicting_values_require_manual_reconciliation() -> None:
    claim = _canonical()
    common = {
        "field_path": "canonical_claim.amount.gross",
        "old_value": 90.0,
        "confidence": 0.9,
        "rationale": "Evidence.",
        "evidence": {},
        "rule_refs": [],
        "issue_ids": ["i1"],
        "issue_codes": ["C1"],
    }
    merged = merge_candidates(
        [
            {**common, "proposed_value": 100.0, "source": "LLM"},
            {**common, "proposed_value": 110.0, "source": "KG"},
        ],
        claim,
    )
    assert merged[0]["source"] == "MANUAL_REQUIRED"


def test_safe_patch_rejects_authoritative_payer_identity() -> None:
    with pytest.raises(UnsafeCorrectionError):
        apply_correction(
            _canonical(),
            field_path="canonical_claim.payer.id",
            expected_old_value="A001",
            proposed_value="A002",
        )


def test_approved_cycle_creates_immutable_new_version_and_payload(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    suggestion_id = cycle["suggestions"][0]["id"]
    service.review(
        claim_id=claim_id,
        suggestion_id=suggestion_id,
        decision="APPROVED",
        reviewer_id="reviewer-1",
        comment="Verified against line items.",
    )
    original = deepcopy(services.repository.get_claim_version(claim_id, 1))
    monkeypatch.setattr(
        "velo_claim.corrections.service.run_claim_validation",
        lambda state, container: {
            **state,
            "validation_report_id": "report-v2",
            "score": 100,
            "final_status": "READY_TO_SUBMIT",
            "payload_status": "READY_TO_SUBMIT",
            "next_agent": "SubmissionAgent",
        },
    )
    result = service.apply(claim_id=claim_id, cycle_id=cycle["cycle"]["id"], reviewer_id="reviewer-1")
    assert result["new_claim_version"] == 2
    assert services.repository.get_claim_version(claim_id, 1) == original
    current = services.repository.get_current_claim_version(claim_id)
    assert current["canonical_claim"]["amount"]["gross"] == 100.0
    assert current["canonical_claim"]["payer"]["eligibility_ref"] == "ELIG-1"
    assert current["canonical_claim"]["pre_auth_ref"] == "PA-1"
    payloads = [row for row in services.repository.claim_payloads if row["claim_id"] == claim_id]
    assert len(payloads) == 2
    assert payloads[0]["object_uri"] != payloads[1]["object_uri"]
    assert result["validation"]["next_agent"] == "SubmissionAgent"


def test_changed_base_version_marks_suggestion_stale(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    suggestion_id = cycle["suggestions"][0]["id"]
    version = services.repository.get_current_claim_version(claim_id)
    services.repository.insert_claim_version(
        claim_id,
        2,
        {
            "canonical_claim": deepcopy(version["canonical_claim"]),
            "route": version["route"],
            "routing_context": version["routing_context"],
            "source_context": version["source_context"],
        },
    )
    with pytest.raises(StaleCorrectionError):
        service.review(
            claim_id=claim_id,
            suggestion_id=suggestion_id,
            decision="APPROVED",
            reviewer_id="reviewer-1",
        )
    assert services.repository.get_correction_suggestion(suggestion_id)["status"] == "STALE"


def test_changed_old_value_marks_suggestion_and_cycle_stale(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    suggestion_id = cycle["suggestions"][0]["id"]
    current = services.repository.get_current_claim_version(claim_id)
    stored = next(
        row
        for row in services.repository.claim_versions
        if row["claim_id"] == claim_id and row["version"] == current["version"]
    )
    stored["canonical_claim"]["amount"]["gross"] = 91.0
    with pytest.raises(StaleCorrectionError):
        service.review(
            claim_id=claim_id,
            suggestion_id=suggestion_id,
            decision="APPROVED",
            reviewer_id="reviewer-1",
        )
    assert services.repository.get_correction_suggestion(suggestion_id)["status"] == "STALE"
    assert services.repository.get_correction_cycle(cycle["cycle"]["id"])["status"] == "STALE"


def test_reviewer_modified_value_is_applied(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    service.review(
        claim_id=claim_id,
        suggestion_id=cycle["suggestions"][0]["id"],
        decision="MODIFIED",
        modified_value=105.0,
        reviewer_id="reviewer-2",
        comment="Contract adjustment verified.",
    )
    monkeypatch.setattr(
        "velo_claim.corrections.service.run_claim_validation",
        lambda state, container: {
            **state,
            "validation_report_id": "report-v2",
            "score": 90,
            "final_status": "READY_TO_SUBMIT",
            "payload_status": "READY_TO_SUBMIT",
            "next_agent": "SubmissionAgent",
        },
    )
    service.apply(claim_id=claim_id, cycle_id=cycle["cycle"]["id"], reviewer_id="reviewer-2")
    current = services.repository.get_current_claim_version(claim_id)
    assert current["canonical_claim"]["amount"]["gross"] == 105.0


def test_manual_suggestion_cannot_be_approved_without_value(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "PAYER_MISMATCH",
                "field": "canonical_claim.payer.id",
                "message": "Payer mismatch.",
                "suggestion": "Reconcile.",
            }
        ],
    )
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    with pytest.raises(InvalidCorrectionStateError):
        service.review(
            claim_id=claim_id,
            suggestion_id=cycle["suggestions"][0]["id"],
            decision="APPROVED",
            reviewer_id="reviewer-1",
        )


def test_rejected_review_replay_does_not_consume_another_cycle(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    suggestion_id = cycle["suggestions"][0]["id"]
    first = service.review(
        claim_id=claim_id,
        suggestion_id=suggestion_id,
        decision="REJECTED",
        reviewer_id="reviewer-1",
        comment="Not accepted.",
    )
    replay = service.review(
        claim_id=claim_id,
        suggestion_id=suggestion_id,
        decision="REJECTED",
        reviewer_id="reviewer-1",
        comment="Network retry.",
    )
    assert first["next_cycle"]["cycle"]["number"] == 2
    assert replay["idempotent"] is True
    assert replay["next_cycle"]["cycle"]["number"] == 2
    assert len(services.repository.list_correction_cycles(claim_id)) == 2


def test_third_rejected_cycle_holds_claim(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    for number in (1, 2, 3):
        assert cycle["cycle"]["number"] == number
        result = service.review(
            claim_id=claim_id,
            suggestion_id=cycle["suggestions"][0]["id"],
            decision="REJECTED",
            reviewer_id=f"reviewer-{number}",
            comment="Rejected after review.",
        )
        if number < 3:
            cycle = result["next_cycle"]
    assert services.repository.get_claim_detail(claim_id)["status"] == "HOLD_CRITICAL"
    assert services.repository.get_correction_cycle(cycle["cycle"]["id"])["status"] == "EXHAUSTED"


def test_approved_database_rule_resolves_without_llm(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "ENCOUNTER_TYPE_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Invalid encounter type.",
                "suggestion": "Use verified source value.",
            }
        ],
    )
    services.repository.correction_rules.append(
        {
            "rule_key": "ENCOUNTER_TYPE_FROM_SOURCE",
            "version": "2.1",
            "issue_code": "ENCOUNTER_TYPE_INVALID",
            "check_type": "METADATA",
            "field_pattern": "canonical_claim.encounter.type",
            "action": {"source_path": "source_context.encounter.type"},
            "status": "ACTIVE",
            "approved_by": "coding-governance",
        }
    )
    llm = TrackingLLM()
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    assert llm.calls == 0
    suggestion = result["correction_suggestions"][0]
    assert suggestion["proposed_value"] == "EMER"
    assert suggestion["rule_refs"][0]["version"] == "2.1"


def test_inactive_database_rule_is_ignored(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "ENCOUNTER_TYPE_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Invalid encounter type.",
                "suggestion": "Review.",
            }
        ],
    )
    services.repository.correction_rules.append(
        {
            "rule_key": "UNAPPROVED_RULE",
            "version": "1",
            "issue_code": "ENCOUNTER_TYPE_INVALID",
            "check_type": "METADATA",
            "field_pattern": "canonical_claim.encounter.type",
            "action": {"value": "EMER"},
            "status": "DRAFT",
            "approved_by": None,
        }
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=DisabledLLM(),
    )
    assert result["correction_suggestions"][0]["source"] == "MANUAL_REQUIRED"


@pytest.mark.parametrize("confidence,proposed", [(0.5, "EMER"), (0.95, "IMP")])
def test_llm_low_confidence_or_unsupported_source_value_is_manual(services, confidence, proposed) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "ENCOUNTER_TYPE_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Invalid encounter type.",
                "suggestion": "Review.",
            }
        ],
    )
    llm = TrackingLLM(
        CorrectionLLMResponse(
            can_suggest=True,
            field_path="canonical_claim.encounter.type",
            current_value="AMB",
            proposed_value=proposed,
            rationale="Model proposal.",
            confidence=confidence,
            evidence_refs=["source:source_context.encounter.type"],
        )
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    assert result["correction_suggestions"][0]["source"] == "MANUAL_REQUIRED"


def test_deterministic_candidate_beats_conflicting_llm() -> None:
    claim = _canonical()
    common = {
        "field_path": "canonical_claim.amount.gross",
        "old_value": 90.0,
        "confidence": 1.0,
        "rationale": "Verified.",
        "evidence": {},
        "rule_refs": [],
        "issue_ids": ["i1"],
        "issue_codes": ["C1"],
    }
    merged = merge_candidates(
        [
            {**common, "proposed_value": 100.0, "source": "RULE_ENGINE"},
            {**common, "proposed_value": 120.0, "source": "LLM", "confidence": 1.0},
        ],
        claim,
    )
    assert merged[0]["source"] == "RULE_ENGINE"
    assert merged[0]["proposed_value"] == 100.0


def test_validation_routes_needs_review_to_correction_agent(services, monkeypatch) -> None:
    claim_id, _ = _seed(services)
    detail = services.repository.get_claim_detail(claim_id)
    issue = CheckIssue(
        code="REVIEW_ME",
        severity=Severity.ERROR,
        check_type="FINANCIAL",
        field="canonical_claim.amount.gross",
        message="Review.",
        suggestion="Correct.",
        penalty=20,
    )
    monkeypatch.setattr(
        "velo_claim.agents.claim_validation.run_validation_checks",
        lambda **kwargs: (kwargs["state"], [CheckResult("FINANCIAL", "FAILED", [issue])]),
    )
    state = run_claim_validation(
        {
            "claim": {"claim_id": claim_id, "version": 1},
            "canonical_claim": detail["canonical_claim"],
            "source_context": detail["source_context"],
            "routing_context": detail["routing_context"],
            "route": detail["route"],
            "claim_payload": "{}",
            "claim_payload_type": "fhir_bundle_json",
            "claim_payload_uri": detail["claim_payload"]["object_uri"],
            "payload_version": 1,
            "errors": [],
            "warnings": [],
        },
        container=services,
    )
    assert state["final_status"] == "NEEDS_REVIEW"
    assert state["next_agent"] == "CorrectionSuggesterAgent"
    assert state["validation_report_id"]


def test_correction_api_requires_auth_and_returns_frontend_shape(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    token = "correction-reviewer-token-at-least-32-characters"
    monkeypatch.setenv("VELO_SUBMISSION_REVIEWERS", json.dumps({token: "reviewer-1"}))
    client = TestClient(create_app(services))
    unauthorized = client.post(f"/claims/{claim_id}/corrections/generate", json={"validation_report_id": report_id})
    assert unauthorized.status_code == 401
    response = client.post(
        f"/claims/{claim_id}/corrections/generate",
        json={"validation_report_id": report_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["claim_id"] == claim_id
    assert body["cycle"]["status"] == "AWAITING_HUMAN_REVIEW"
    assert body["suggestions"][0]["field_path"] == "canonical_claim.amount.gross"
    assert body["suggestions"][0]["can_modify"] is True


def test_noncanonical_manual_suggestion_is_not_editable(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "PAYLOAD_CONFORMITY",
                "severity": "WARNING",
                "code": "XSD_NOT_CONFIGURED",
                "field": "schema",
                "message": "Schema configuration is missing.",
                "suggestion": "Configure the schema.",
            }
        ],
    )
    cycle = CorrectionWorkflowService(services).generate(claim_id, validation_report_id=report_id)
    assert cycle["suggestions"][0]["source"] == "MANUAL_REQUIRED"
    assert cycle["suggestions"][0]["can_modify"] is False


def test_exact_requested_validation_report_is_loaded(services) -> None:
    claim_id, first_report = _seed(services)
    second_report = services.repository.insert_validation_report(
        claim_id,
        {"version": 1, "score": 70, "final_status": "NEEDS_REVIEW", "report": {"status": "NEEDS_REVIEW"}},
    )
    services.repository.insert_validation_issue(
        second_report,
        {
            "check_type": "METADATA",
            "severity": "ERROR",
            "code": "PAYER_MISMATCH",
            "field": "canonical_claim.payer.id",
            "message": "Different report.",
            "suggestion": "Review.",
        },
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": first_report},
        container=services,
        llm_client=DisabledLLM(),
    )
    assert result["validation_report_id"] == first_report
    assert result["correction_suggestions"][0]["issue_codes"] == ["FINANCIAL_GROSS_MISMATCH"]


def test_same_value_candidates_merge_evidence_and_issue_ids() -> None:
    claim = _canonical()
    common = {
        "field_path": "canonical_claim.amount.gross",
        "old_value": 90.0,
        "proposed_value": 100.0,
        "confidence": 0.9,
        "rationale": "Verified.",
        "rule_refs": [],
    }
    merged = merge_candidates(
        [
            {**common, "source": "LLM", "evidence": {"a": 1}, "issue_ids": ["i1"], "issue_codes": ["C1"]},
            {**common, "source": "KG", "evidence": {"b": 2}, "issue_ids": ["i2"], "issue_codes": ["C2"]},
        ],
        claim,
    )
    assert len(merged) == 1
    assert merged[0]["source"] == "MIXED"
    assert merged[0]["issue_ids"] == ["i1", "i2"]
    assert len(merged[0]["evidence"]["sources"]) == 2


def test_llm_receives_only_relevant_source_candidates(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "ENCOUNTER_TYPE_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Invalid encounter type.",
                "suggestion": "Review.",
            }
        ],
    )
    llm = TrackingLLM(
        CorrectionLLMResponse(
            can_suggest=True,
            field_path="canonical_claim.encounter.type",
            current_value="AMB",
            proposed_value="EMER",
            rationale="Source-backed.",
            confidence=0.9,
            evidence_refs=["source:source_context.encounter.type"],
        )
    )
    run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    serialized = json.dumps(llm.contexts[0])
    assert "source_context.encounter.type" in serialized
    assert "Test Patient" not in serialized
    assert "national_identifier" not in serialized


def test_protected_issue_never_calls_llm(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "PAYER_MISMATCH",
                "field": "canonical_claim.payer.id",
                "message": "Payer mismatch.",
                "suggestion": "Review.",
            }
        ],
    )
    llm = TrackingLLM()
    run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    assert llm.calls == 0


def test_kg_unavailable_is_evidence_visible_and_manual(services, monkeypatch) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "CODING",
                "severity": "ERROR",
                "code": "DIAGNOSIS_PROCEDURE_KNOWLEDGE_UNKNOWN",
                "field": "canonical_claim.procedures.99213",
                "message": "Unknown compatibility.",
                "suggestion": "Review coding.",
            }
        ],
    )
    monkeypatch.setattr(
        services.kg_client,
        "query_diagnosis_procedure_compatibility",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Neo4j unavailable")),
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=DisabledLLM(),
    )
    suggestion = result["correction_suggestions"][0]
    assert suggestion["source"] == "MANUAL_REQUIRED"
    assert suggestion["evidence"]["kg"]["unavailable"]["reason"] == "RuntimeError"


def test_production_graph_rejects_mock_kg(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    monkeypatch.setenv("VELO_CLAIM_STORAGE", "production")
    with pytest.raises(CorrectionContextError):
        run_correction_suggester(
            {"claim_id": claim_id, "validation_report_id": report_id},
            container=services,
            llm_client=DisabledLLM(),
        )


def test_invalid_medgemma_json_is_rejected(monkeypatch) -> None:
    client = CorrectionLLMClient(
        enabled=True,
        base_url="http://medgemma.test/v1",
        api_key="",
        model="medgemma",
        api_style="openai_chat",
        generate_path="/generate",
        timeout_seconds=1,
        minimum_confidence=0.8,
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"not-json"}}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    with pytest.raises(json.JSONDecodeError):
        client.resolve({"issue": "test"})


def test_medgemma_dgx_chat_response_is_supported(monkeypatch) -> None:
    client = CorrectionLLMClient(
        enabled=True,
        base_url="http://model-server.test/v1",
        api_key="",
        model="medgemma-4b-it",
        api_style="openai_chat",
        generate_path="/generate",
        timeout_seconds=1,
        minimum_confidence=0.8,
    )
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            content = json.dumps(
                {
                    "can_suggest": True,
                    "field_path": "canonical_claim.encounter.type",
                    "current_value": "AMB",
                    "proposed_value": "EMER",
                    "rationale": "Supported by the source encounter.",
                    "confidence": 0.95,
                    "evidence_refs": ["source:source_context.encounter.type"],
                    "rule_refs": [],
                    "requires_manual_reconciliation": False,
                }
            )
            return json.dumps({"role": "assistant", "content": f"```json\n{content}\n```"}).encode()

    def open_request(request, timeout):
        captured.update(json.loads(request.data.decode()))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    result = client.resolve({"issue": "test"})
    assert result.proposed_value == "EMER"
    assert captured["stream"] is False
    assert captured["temperature"] == 0.01


def test_cycle_response_explains_reviewer_input(services) -> None:
    canonical = _canonical()
    canonical["encounter"]["id"] = None
    claim_id, report_id = _seed(
        services,
        canonical=canonical,
        issues=[
            {
                "check_type": "DOCUMENTATION",
                "severity": "ERROR",
                "code": "ENCOUNTER_MISSING",
                "field": "canonical_claim.encounter.id",
                "message": "Encounter reference is missing.",
                "suggestion": "Enter the source encounter ID.",
            }
        ],
    )
    cycle = CorrectionWorkflowService(services).generate(claim_id, validation_report_id=report_id)
    suggestion = cycle["suggestions"][0]
    assert suggestion["can_modify"] is True
    assert suggestion["resolution"]["kind"] == "REVIEWER_INPUT"
    assert suggestion["resolution"]["action_label"] == "Enter encounter ID"


def test_cycle_response_explains_external_setup_action(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "PAYLOAD_CONFORMITY",
                "severity": "WARNING",
                "code": "XSD_NOT_CONFIGURED",
                "field": "schema",
                "message": "Schema is not configured.",
                "suggestion": "Configure the XSD path.",
            }
        ],
    )
    cycle = CorrectionWorkflowService(services).generate(claim_id, validation_report_id=report_id)
    suggestion = cycle["suggestions"][0]
    assert suggestion["can_modify"] is False
    assert suggestion["resolution"]["kind"] == "EXTERNAL_ACTION"
    assert suggestion["resolution"]["title"] == "Configure ECLAIMLINK validation"


def test_force_new_api_reanalyzes_into_next_cycle(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    token = "correction-reviewer-token-at-least-32-characters"
    monkeypatch.setenv("VELO_SUBMISSION_REVIEWERS", json.dumps({token: "reviewer-1"}))
    client = TestClient(create_app(services))
    headers = {"Authorization": f"Bearer {token}"}
    first = client.post(
        f"/claims/{claim_id}/corrections/generate",
        json={"validation_report_id": report_id},
        headers=headers,
    )
    second = client.post(
        f"/claims/{claim_id}/corrections/generate",
        json={"validation_report_id": report_id, "force_new": True},
        headers=headers,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["cycle"]["number"] == 2


def test_reviewed_unknown_coding_pair_does_not_loop(services) -> None:
    state = {
        "canonical_claim": _canonical(),
        "correction_reviewed_coding_codes": ["99213"],
    }
    result = check_coding_consistency(state, services.kg_client)
    assert result.status == "PASS"
    assert result.issues == []


def test_rejected_cycle_never_mutates_claim_or_payload(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    before_version = deepcopy(services.repository.get_current_claim_version(claim_id))
    before_payload = deepcopy(services.repository.latest_claim_payload(claim_id))
    cycle = service.generate(claim_id, validation_report_id=report_id)
    service.review(
        claim_id=claim_id,
        suggestion_id=cycle["suggestions"][0]["id"],
        decision="REJECTED",
        reviewer_id="reviewer-1",
        comment="Rejected.",
    )
    assert services.repository.get_current_claim_version(claim_id) == before_version
    assert services.repository.latest_claim_payload(claim_id) == before_payload


def test_cycle_with_multiple_fields_applies_atomically(services, monkeypatch) -> None:
    canonical = _canonical()
    canonical["amount"]["net"] = 80.0
    claim_id, report_id = _seed(
        services,
        canonical=canonical,
        issues=[
            {
                "check_type": "FINANCIAL",
                "severity": "ERROR",
                "code": "FINANCIAL_GROSS_MISMATCH",
                "field": "canonical_claim.amount.gross",
                "message": "Gross mismatch.",
                "suggestion": "Recalculate.",
            },
            {
                "check_type": "FINANCIAL",
                "severity": "ERROR",
                "code": "FINANCIAL_NET_MISMATCH",
                "field": "canonical_claim.amount.net",
                "message": "Net mismatch.",
                "suggestion": "Recalculate.",
            },
        ],
    )
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    assert len(cycle["suggestions"]) == 2
    for suggestion in cycle["suggestions"]:
        service.review(
            claim_id=claim_id,
            suggestion_id=suggestion["id"],
            decision="APPROVED",
            reviewer_id="reviewer-1",
        )
    monkeypatch.setattr(
        "velo_claim.corrections.service.run_claim_validation",
        lambda state, container: {
            **state,
            "validation_report_id": "v2",
            "score": 100,
            "final_status": "READY_TO_SUBMIT",
            "payload_status": "READY_TO_SUBMIT",
            "next_agent": "SubmissionAgent",
        },
    )
    service.apply(claim_id=claim_id, cycle_id=cycle["cycle"]["id"], reviewer_id="reviewer-1")
    amount = services.repository.get_current_claim_version(claim_id)["canonical_claim"]["amount"]
    assert amount["gross"] == 100.0
    assert amount["net"] == 100.0


def test_payload_build_failure_keeps_old_version_and_cycle_retryable(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    service.review(
        claim_id=claim_id,
        suggestion_id=cycle["suggestions"][0]["id"],
        decision="APPROVED",
        reviewer_id="reviewer-1",
    )
    monkeypatch.setattr(
        service.builder,
        "build_payload_from_canonical",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    with pytest.raises(RuntimeError):
        service.apply(claim_id=claim_id, cycle_id=cycle["cycle"]["id"], reviewer_id="reviewer-1")
    assert services.repository.get_current_claim_version(claim_id)["version"] == 1
    assert services.repository.get_correction_cycle(cycle["cycle"]["id"])["status"] == "READY_TO_APPLY"


def test_payload_from_canonical_preserves_corrected_value_and_references(services) -> None:
    claim_id, _ = _seed(services)
    detail = services.repository.get_claim_detail(claim_id)
    canonical = deepcopy(detail["canonical_claim"])
    canonical["amount"]["net"] = 123.0
    builder = ClaimBuilderModule(
        repository=services.repository,
        object_store=services.object_store,
        kg_client=services.kg_client,
        payer_rule_loader=services.payer_rule_loader,
    )
    state = builder.build_payload_from_canonical(
        {
            "claim": {"claim_id": claim_id},
            "route": detail["route"],
            "routing_context": detail["routing_context"],
            "source_context": detail["source_context"],
            "payload_version": 1,
        },
        canonical,
        persist=False,
    )
    payload = json.loads(state["claim_payload"])
    claim_resource = next(item["resource"] for item in payload["entry"] if item["resource"]["resourceType"] == "Claim")
    assert claim_resource["total"]["value"] == 123.0
    assert claim_resource["insurance"][0]["preAuthRef"] == ["PA-1"]
    assert state["canonical_claim"]["payer"]["eligibility_ref"] == "ELIG-1"


def test_apply_requires_all_suggestions_reviewed(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    with pytest.raises(InvalidCorrectionStateError):
        service.apply(claim_id=claim_id, cycle_id=cycle["cycle"]["id"], reviewer_id="reviewer-1")


def test_domain_audit_events_record_reviewer_actor(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    service.review(
        claim_id=claim_id,
        suggestion_id=cycle["suggestions"][0]["id"],
        decision="APPROVED",
        reviewer_id="reviewer-audit",
    )
    events = [str(item.get("event_type")) for item in services.repository.audit_events]
    assert "CORRECTION_CYCLE_CREATED" in events
    assert "CORRECTION_SUGGESTION_CREATED" in events
    review_event = next(item for item in services.repository.audit_events if str(item.get("event_type")) == "CORRECTION_REVIEW_APPROVED")
    assert review_event["payload"]["reviewer_id"] == "reviewer-audit"


def test_api_returns_409_for_stale_review(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    token = "correction-reviewer-token-at-least-32-characters"
    monkeypatch.setenv("VELO_SUBMISSION_REVIEWERS", json.dumps({token: "reviewer-1"}))
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    suggestion_id = cycle["suggestions"][0]["id"]
    current = services.repository.get_current_claim_version(claim_id)
    services.repository.insert_claim_version(
        claim_id,
        2,
        {
            "canonical_claim": deepcopy(current["canonical_claim"]),
            "route": current["route"],
            "routing_context": current["routing_context"],
            "source_context": current["source_context"],
        },
    )
    response = TestClient(create_app(services)).post(
        f"/claims/{claim_id}/corrections/{suggestion_id}/review",
        json={"decision": "APPROVED"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409


def test_generation_does_not_mark_claim_ready_to_submit(services) -> None:
    claim_id, report_id = _seed(services)
    result = CorrectionWorkflowService(services).generate(claim_id, validation_report_id=report_id)
    assert result["cycle"]["status"] == "AWAITING_HUMAN_REVIEW"
    assert services.repository.get_claim_detail(claim_id)["status"] == "NEEDS_REVIEW"


def test_llm_wrong_field_path_is_manual(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "ENCOUNTER_TYPE_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Invalid encounter type.",
                "suggestion": "Review.",
            }
        ],
    )
    llm = TrackingLLM(
        CorrectionLLMResponse(
            can_suggest=True,
            field_path="canonical_claim.patient.id",
            current_value="PAT-1",
            proposed_value="PAT-2",
            rationale="Unsafe.",
            confidence=0.99,
            evidence_refs=["source:source_context.encounter.type"],
        )
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    assert result["correction_suggestions"][0]["source"] == "MANUAL_REQUIRED"


def test_llm_without_evidence_refs_is_manual(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "METADATA",
                "severity": "ERROR",
                "code": "ENCOUNTER_TYPE_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Invalid encounter type.",
                "suggestion": "Review.",
            }
        ],
    )
    llm = TrackingLLM(
        CorrectionLLMResponse(
            can_suggest=True,
            field_path="canonical_claim.encounter.type",
            current_value="AMB",
            proposed_value="EMER",
            rationale="No evidence.",
            confidence=0.99,
            evidence_refs=[],
        )
    )
    result = run_correction_suggester(
        {"claim_id": claim_id, "validation_report_id": report_id},
        container=services,
        llm_client=llm,
    )
    assert result["correction_suggestions"][0]["source"] == "MANUAL_REQUIRED"


def test_rejected_value_is_not_proposed_again(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    first = service.generate(claim_id, validation_report_id=report_id)
    rejected_value = first["suggestions"][0]["proposed_value"]
    result = service.review(
        claim_id=claim_id,
        suggestion_id=first["suggestions"][0]["id"],
        decision="REJECTED",
        reviewer_id="reviewer-1",
        comment="Rejected.",
    )
    replacement = result["next_cycle"]["suggestions"][0]
    assert replacement["source"] == "MANUAL_REQUIRED"
    assert replacement["proposed_value"] != rejected_value


def test_stale_validation_report_version_is_rejected(services) -> None:
    claim_id, _ = _seed(services)
    stale_report = services.repository.insert_validation_report(
        claim_id,
        {"version": 0, "score": 70, "final_status": "NEEDS_REVIEW", "report": {"status": "NEEDS_REVIEW"}},
    )
    services.repository.insert_validation_issue(
        stale_report,
        {
            "check_type": "FINANCIAL",
            "severity": "ERROR",
            "code": "FINANCIAL_GROSS_MISMATCH",
            "field": "canonical_claim.amount.gross",
            "message": "Old report.",
            "suggestion": "Review.",
        },
    )
    with pytest.raises(CorrectionContextError):
        run_correction_suggester(
            {"claim_id": claim_id, "validation_report_id": stale_report},
            container=services,
            llm_client=DisabledLLM(),
        )


def test_reviewer_modified_value_must_keep_field_type(services) -> None:
    claim_id, report_id = _seed(services)
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    with pytest.raises(UnsafeCorrectionError):
        service.review(
            claim_id=claim_id,
            suggestion_id=cycle["suggestions"][0]["id"],
            decision="MODIFIED",
            modified_value="one hundred",
            reviewer_id="reviewer-1",
        )


def test_correction_api_lists_and_gets_persisted_cycle(services, monkeypatch) -> None:
    claim_id, report_id = _seed(services)
    token = "correction-reviewer-token-at-least-32-characters"
    monkeypatch.setenv("VELO_SUBMISSION_REVIEWERS", json.dumps({token: "reviewer-1"}))
    service = CorrectionWorkflowService(services)
    cycle = service.generate(claim_id, validation_report_id=report_id)
    client = TestClient(create_app(services))
    headers = {"Authorization": f"Bearer {token}"}
    listed = client.get(f"/claims/{claim_id}/corrections", headers=headers)
    fetched = client.get(
        f"/claims/{claim_id}/corrections/{cycle['cycle']['id']}",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert fetched.status_code == 200
    assert fetched.json()["cycle"]["id"] == cycle["cycle"]["id"]


def test_medgemma_contract_rejects_extra_and_coerced_fields() -> None:
    payload = {
        "can_suggest": "true",
        "field_path": "canonical_claim.encounter.type",
        "current_value": "AMB",
        "proposed_value": "EMER",
        "rationale": "Supported by the encounter source.",
        "confidence": 0.95,
        "evidence_refs": ["source:source_context.encounter.type"],
        "rule_refs": [],
        "requires_manual_reconciliation": False,
        "unexpected": "not allowed",
    }
    with pytest.raises(ValidationError):
        CorrectionLLMResponse.model_validate(payload)


def test_llm_receives_only_the_payer_rule_named_by_issue_evidence(services) -> None:
    claim_id, report_id = _seed(
        services,
        issues=[
            {
                "check_type": "PAYER_RULES",
                "severity": "ERROR",
                "code": "PAYER_FIELD_INVALID",
                "field": "canonical_claim.encounter.type",
                "message": "Encounter type does not satisfy the payer rule.",
                "suggestion": "Reconcile against the source encounter.",
                "evidence": {"rule_id": "RULE-EXACT"},
            }
        ],
    )
    llm = TrackingLLM(
        CorrectionLLMResponse(
            can_suggest=True,
            field_path="canonical_claim.encounter.type",
            current_value="AMB",
            proposed_value="EMER",
            rationale="The source encounter records emergency care.",
            confidence=0.95,
            evidence_refs=["source:source_context.encounter.type"],
            rule_refs=[{"rule_id": "RULE-EXACT"}],
            requires_manual_reconciliation=False,
        )
    )
    run_correction_suggester(
        {
            "claim_id": claim_id,
            "validation_report_id": report_id,
            "payer_rule_set": {
                "source_version": "test-v1",
                "rules": [
                    {"rule_id": "RULE-OTHER", "rule_type": "OTHER"},
                    {"rule_id": "RULE-EXACT", "rule_type": "ENCOUNTER"},
                ],
            },
        },
        container=services,
        llm_client=llm,
    )
    assert llm.contexts[0]["payer_rules"] == [
        {"rule_id": "RULE-EXACT", "rule_type": "ENCOUNTER"}
    ]
