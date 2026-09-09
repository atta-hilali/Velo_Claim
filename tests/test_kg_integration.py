from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from velo_claim.api.app import create_app
from velo_claim.checks.coding import check_coding_consistency
from velo_claim.checks.orchestrator import calculate_validation_report
from velo_claim.core.container import _build_kg_client, build_default_container
from velo_claim.core.enums import ValidationStatus
from velo_claim.kg.json_client import JsonKnowledgeGraphClient
from velo_claim.kg.mock import MockNeo4jClient
from velo_claim.kg.models import KnowledgeStatus
from velo_claim.kg.models import KnowledgeResult
from velo_claim.kg.neo4j import Neo4jKnowledgeGraphClient


def test_json_unknown_pa_is_not_a_negative_decision() -> None:
    client = JsonKnowledgeGraphClient(Path(__file__).resolve().parents[1] / "data" / "coding_knowledge_graph.json")
    result = client.query_prior_authorization(
        payer_id="UNKNOWN",
        plan_id="UNKNOWN",
        procedure_code="D9999",
        procedure_system="CDT",
    )
    assert result.status == KnowledgeStatus.UNKNOWN
    assert client.query_pa_required("UNKNOWN", "UNKNOWN", "D9999") is None


def test_mock_generic_query_preserves_cdt() -> None:
    result = MockNeo4jClient().query_diagnosis_procedure_compatibility(
        diagnosis_code="K02.9",
        diagnosis_system="ICD-10-CM",
        procedure_code="D2391",
        procedure_system="CDT",
    )
    assert result.procedure_system == "CDT"
    assert result.status == KnowledgeStatus.UNKNOWN


def test_explicit_mock_backend_is_the_only_mock_selection(monkeypatch) -> None:
    monkeypatch.setenv("VALIDATION_KG_BACKEND", "mock")
    assert isinstance(_build_kg_client(Path.cwd()), MockNeo4jClient)


def test_missing_production_backend_is_rejected(monkeypatch) -> None:
    monkeypatch.delenv("VALIDATION_KG_BACKEND", raising=False)
    with pytest.raises(ValueError, match="explicit VALIDATION_KG_BACKEND"):
        _build_kg_client(Path.cwd())


def test_health_identifies_explicit_mock_backend() -> None:
    with TestClient(create_app(build_default_container())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    diagnostics = response.json()["knowledge_graph"]
    assert diagnostics["backend"] == "mock"
    assert diagnostics["mock_fallback"] is True
    assert diagnostics["production_safe"] is False


def test_unavailable_neo4j_returns_unavailable_not_pass() -> None:
    client = Neo4jKnowledgeGraphClient(
        uri="bolt://127.0.0.1:1",
        user="neo4j",
        password="not-a-real-password",
        query_timeout_seconds=0.2,
    )
    try:
        result = client.query_plan_benefit(
            payer_id="DAMAN",
            plan_id="DAMAN-ENHANCED",
            procedure_code="D0310",
            procedure_system="CDT",
        )
    finally:
        client.close()
    assert result.status == KnowledgeStatus.UNAVAILABLE


def test_unknown_coding_knowledge_routes_the_claim_to_review() -> None:
    class UnknownProductionKnowledge(JsonKnowledgeGraphClient):
        def query_diagnosis_procedure_compatibility(self, **kwargs) -> KnowledgeResult:
            return KnowledgeResult(
                status=KnowledgeStatus.UNKNOWN,
                query="diagnosis_procedure_compatibility",
                source="NEO4J",
                diagnosis_code=kwargs["diagnosis_code"],
                diagnosis_system=kwargs["diagnosis_system"],
                procedure_code=kwargs["procedure_code"],
                procedure_system=kwargs["procedure_system"],
            )

    coding = check_coding_consistency(
        {
            "canonical_claim": {
                "diagnoses": [{"code": "E11.9", "system": "ICD-10-CM"}],
                "procedures": [{"code": "83036", "system": "CPT"}],
            }
        },
        UnknownProductionKnowledge(Path(__file__).resolve().parents[1] / "data" / "coding_knowledge_graph.json"),
    )
    report = calculate_validation_report("CLM-KG-UNKNOWN", [coding])
    assert coding.status == "REVIEW_REQUIRED"
    assert report.status == ValidationStatus.NEEDS_REVIEW


def _live_client() -> Neo4jKnowledgeGraphClient:
    uri = os.getenv("VELO_TEST_NEO4J_URI")
    password = os.getenv("VELO_TEST_NEO4J_PASSWORD")
    if not uri or not password:
        pytest.skip("Set VELO_TEST_NEO4J_URI and VELO_TEST_NEO4J_PASSWORD for live KG tests.")
    return Neo4jKnowledgeGraphClient(
        uri=uri,
        user=os.getenv("VELO_TEST_NEO4J_USER", "neo4j"),
        password=password,
        database=os.getenv("VELO_TEST_NEO4J_DATABASE", "neo4j"),
    )


def test_live_daman_d0310_plan_benefit() -> None:
    client = _live_client()
    try:
        assert client.verify_connectivity()
        result = client.query_plan_benefit(
            payer_id="DAMAN",
            plan_id="DAMAN-ENHANCED",
            procedure_code="D0310",
            procedure_system="CDT",
            service_date="2026-06-15",
        )
    finally:
        client.close()
    assert result.status == KnowledgeStatus.SUPPORTED
    assert result.source == "NEO4J"
    assert result.entity_id == "DAMAN-ENHANCED-D0310-CDT"
    assert result.evidence["benefit"]["covered"] is True
    assert result.evidence["benefit"]["prior_auth_required"] is False
    assert result.evidence["benefit"]["waiting_period_days"] == 0


def test_live_unknown_plan_and_old_mock_fact_stay_unknown() -> None:
    client = _live_client()
    try:
        unknown_plan = client.query_plan_benefit(
            payer_id="DAMAN",
            plan_id="NOT-A-REAL-PLAN",
            procedure_code="D0310",
            procedure_system="CDT",
        )
        old_mock_fact = client.query_diagnosis_procedure_compatibility(
            diagnosis_code="E11.9",
            diagnosis_system="ICD-10-CM",
            procedure_code="83036",
            procedure_system="CPT",
        )
    finally:
        client.close()
    assert unknown_plan.status == KnowledgeStatus.UNKNOWN
    assert old_mock_fact.status == KnowledgeStatus.UNKNOWN
    assert old_mock_fact.source == "NEO4J"


def test_live_cdt_coding_result_contains_neo4j_evidence() -> None:
    client = _live_client()
    try:
        result = check_coding_consistency(
            {
                "canonical_claim": {
                    "diagnoses": [{"code": "A69.1", "system": "ICD-10-CM"}],
                    "procedures": [{"code": "D4342", "system": "CDT", "service_date": "2026-06-15"}],
                }
            },
            client,
        )
    finally:
        client.close()
    assert result.status == "PASS"
    assert result.data["kg_results"][0]["source"] == "NEO4J"
    assert result.data["kg_results"][0]["procedure_system"] == "CDT"
