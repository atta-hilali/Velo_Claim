from __future__ import annotations

import os
from typing import Any

from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface


class Neo4jKnowledgeGraphClient(Neo4jClientInterface):
    """Parameterized, read-only Neo4j adapter for coding and payer knowledge."""

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
    ) -> None:
        if not uri or not user or not password:
            raise ValueError("NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD are required.")
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise RuntimeError("Install the 'neo4j' package to use the Neo4j KG backend.") from exc
        self.database = database
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    @classmethod
    def from_env(cls) -> "Neo4jKnowledgeGraphClient":
        return cls(
            uri=os.getenv("NEO4J_URI", ""),
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", ""),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()

    def close(self) -> None:
        self._driver.close()

    def _single(self, query: str, **parameters: Any) -> dict[str, Any] | None:
        records, _, _ = self._driver.execute_query(
            query,
            parameters_=parameters,
            database_=self.database,
            routing_="r",
        )
        return dict(records[0]) if records else None

    def query_icd_cpt_compatibility(self, icd_code: str, cpt_code: str) -> bool | None:
        row = self._single(
            """
            MATCH (diagnosis)-[relationship]-(procedure)
            WHERE toUpper(coalesce(diagnosis.code, diagnosis.Code, '')) = $icd_code
              AND toUpper(coalesce(procedure.code, procedure.Code, '')) = $procedure_code
              AND any(label IN labels(diagnosis) WHERE label IN
                  ['Diagnosis_Code', 'DiagnosisCode', 'ICDCode'])
              AND any(label IN labels(procedure) WHERE label IN
                  ['Dental_Procedure_Code', 'Procedure_Code', 'ProcedureCode', 'CPTCode'])
            RETURN type(relationship) AS relationship_type,
                   coalesce(relationship.compatible, relationship.supports, true) AS compatible
            LIMIT 1
            """,
            icd_code=normalize_code(icd_code),
            procedure_code=normalize_code(cpt_code),
        )
        return None if row is None else bool(row.get("compatible", True))

    def query_pa_required(self, payer_id: str, plan_id: str, cpt_code: str) -> bool:
        row = self._single(
            """
            MATCH (procedure)
            WHERE toUpper(coalesce(procedure.code, procedure.Code, '')) = $procedure_code
            OPTIONAL MATCH (payer)-[rule]->(procedure)
            WHERE any(label IN labels(payer) WHERE label IN ['Payer', 'Plan', 'InsurancePlan'])
              AND toUpper(coalesce(payer.payer_id, payer.id, '*')) IN ['*', $payer_id]
              AND toUpper(coalesce(payer.plan_id, payer.plan, '*')) IN ['*', $plan_id]
              AND type(rule) IN ['REQUIRES_PRIOR_AUTH', 'PRIOR_AUTH_REQUIRED', 'COVERS']
            RETURN coalesce(rule.required, procedure.prior_auth_required, false) AS required
            LIMIT 1
            """,
            payer_id=normalize_code(payer_id),
            plan_id=normalize_code(plan_id),
            procedure_code=normalize_code(cpt_code),
        )
        return bool(row and row.get("required"))

    def query_bundled_procedures(self, cpt_code: str) -> list[str]:
        row = self._single(
            """
            MATCH (procedure)-[relationship]->(bundled)
            WHERE toUpper(coalesce(procedure.code, procedure.Code, '')) = $procedure_code
              AND type(relationship) IN ['BUNDLES', 'BUNDLED_WITH', 'INCLUDES']
            RETURN collect(DISTINCT toUpper(coalesce(bundled.code, bundled.Code))) AS codes
            """,
            procedure_code=normalize_code(cpt_code),
        )
        return sorted({str(item) for item in (row or {}).get("codes", []) if item})

    def query_required_documents(self, cpt_code: str) -> list[str]:
        row = self._single(
            """
            MATCH (procedure)
            WHERE toUpper(coalesce(procedure.code, procedure.Code, '')) = $procedure_code
            OPTIONAL MATCH (procedure)-[:REQUIRES_DOCUMENT|REQUIRES_DOCUMENTATION]->(document)
            WITH procedure, collect(DISTINCT coalesce(document.code, document.name)) AS linked
            RETURN linked + coalesce(procedure.required_docs, []) AS documents
            """,
            procedure_code=normalize_code(cpt_code),
        )
        values = (row or {}).get("documents", [])
        if isinstance(values, str):
            values = [item.strip() for item in values.split(",")]
        return sorted({normalize_code(item) for item in values if item and str(item).lower() != "nan"})
