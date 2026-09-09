from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from typing import Any

from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.models import KnowledgeResult, KnowledgeStatus, normalize_code_system


logger = logging.getLogger(__name__)


class KnowledgeGraphUnavailable(RuntimeError):
    pass


class Neo4jKnowledgeGraphClient(Neo4jClientInterface):
    """Read-only adapter for the populated Velo Claim Neo4j graph."""

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
        query_timeout_seconds: float = 10.0,
    ) -> None:
        if not uri or not user or not password:
            raise ValueError("NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD are required.")
        try:
            from neo4j import GraphDatabase, Query, READ_ACCESS
        except ImportError as exc:
            raise RuntimeError("Install the 'neo4j' package to use the Neo4j KG backend.") from exc
        self.backend = "neo4j"
        self.database = database
        self.query_timeout_seconds = query_timeout_seconds
        self._query_type = Query
        self._read_access = READ_ACCESS
        self._driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=float(os.getenv("NEO4J_CONNECTION_TIMEOUT_SECONDS", "10")),
            max_connection_lifetime=int(os.getenv("NEO4J_MAX_CONNECTION_LIFETIME_SECONDS", "300")),
        )
        self._last_error: str | None = None

    @classmethod
    def from_env(cls) -> "Neo4jKnowledgeGraphClient":
        return cls(
            uri=os.getenv("NEO4J_URI", ""),
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", ""),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
            query_timeout_seconds=float(os.getenv("NEO4J_QUERY_TIMEOUT_SECONDS", "10")),
        )

    def verify_connectivity(self, *, raise_on_error: bool = True) -> bool:
        try:
            self._driver.verify_connectivity()
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = _safe_error(exc)
            logger.error("Neo4j connectivity verification failed: %s", self._last_error)
            if raise_on_error:
                raise KnowledgeGraphUnavailable(self._last_error) from exc
            return False

    def close(self) -> None:
        self._driver.close()

    def diagnostics(self) -> dict:
        healthy = self.verify_connectivity(raise_on_error=False)
        return {
            "backend": "neo4j",
            "connectivity": "healthy" if healthy else "unhealthy",
            "database": self.database,
            "mock_fallback": False,
            "production_safe": healthy,
            **({"error": self._last_error} if self._last_error else {}),
        }

    def query_diagnosis_procedure_compatibility(
        self, *, diagnosis_code: str, procedure_code: str, diagnosis_system: str,
        procedure_system: str, service_date: str | None = None,
    ) -> KnowledgeResult:
        diagnosis = normalize_code(diagnosis_code)
        procedure = normalize_code(procedure_code)
        diagnosis_system = normalize_code_system(diagnosis_system, diagnosis=True)
        procedure_system = normalize_code_system(procedure_system)
        base = dict(
            query="diagnosis_procedure_compatibility", source="NEO4J",
            diagnosis_code=diagnosis, diagnosis_system=diagnosis_system,
            procedure_code=procedure, procedure_system=procedure_system,
        )
        try:
            rows = self._read(
                """
                OPTIONAL MATCH (diagnosis:Diagnosis_Code)
                WHERE toUpper(diagnosis.code) = $diagnosis_code
                  AND toUpper(diagnosis.code_system) = $diagnosis_system
                WITH head(collect(diagnosis)) AS diagnosis
                OPTIONAL MATCH (procedure:Dental_Procedure_Code)
                WHERE toUpper(procedure.code) = $procedure_code
                  AND toUpper(procedure.code_system) = $procedure_system
                  AND ($service_date IS NULL OR
                       (coalesce(procedure.valid_from, date('0001-01-01')) <= date($service_date)
                        AND coalesce(procedure.valid_to, date('9999-12-31')) >= date($service_date)))
                WITH diagnosis, head(collect(procedure)) AS procedure
                OPTIONAL MATCH (diagnosis)-[relationship:TRIGGERS]->(procedure)
                RETURN diagnosis IS NOT NULL AS diagnosis_found,
                       procedure IS NOT NULL AS procedure_found,
                       relationship IS NOT NULL AS supported,
                       properties(relationship) AS relationship_properties,
                       diagnosis.code AS diagnosis_code,
                       diagnosis.version AS diagnosis_version,
                       procedure.code AS procedure_code,
                       procedure.version AS procedure_version
                """,
                diagnosis_code=diagnosis,
                diagnosis_system=diagnosis_system,
                procedure_code=procedure,
                procedure_system=procedure_system,
                service_date=_date_text(service_date),
            )
        except KnowledgeGraphUnavailable as exc:
            return KnowledgeResult(status=KnowledgeStatus.UNAVAILABLE, reason=str(exc), **base)
        row = rows[0] if rows else {}
        status = KnowledgeStatus.SUPPORTED if row.get("supported") else KnowledgeStatus.UNKNOWN
        reason = None if row.get("supported") else "No explicit TRIGGERS relationship was found; absence is not incompatibility."
        return KnowledgeResult(status=status, reason=reason, evidence=row, **base)

    def query_plan_benefit(
        self, *, payer_id: str, plan_id: str, procedure_code: str,
        procedure_system: str, service_date: str | None = None,
        employer_policy_id: str | None = None,
    ) -> KnowledgeResult:
        payer = normalize_code(payer_id)
        plan = normalize_code(plan_id)
        procedure = normalize_code(procedure_code)
        system = normalize_code_system(procedure_system)
        base = dict(
            query="plan_benefit", source="NEO4J", payer_id=payer, plan_id=plan,
            procedure_code=procedure, procedure_system=system,
        )
        try:
            rows = self._read(
                """
                MATCH (plan:Insurance_Plan)-[:HAS_BENEFIT]->(benefit:Plan_Benefit)
                      -[:GOVERNS]->(procedure:Dental_Procedure_Code)
                WHERE toUpper(plan.plan_id) = $plan_id
                  AND toUpper(plan.payer_id) IN $payer_ids
                  AND toUpper(procedure.code) = $procedure_code
                  AND toUpper(procedure.code_system) = $procedure_system
                  AND toUpper(benefit.code_system) = $procedure_system
                  AND ($service_date IS NULL OR
                       (coalesce(procedure.valid_from, date('0001-01-01')) <= date($service_date)
                        AND coalesce(procedure.valid_to, date('9999-12-31')) >= date($service_date)))
                OPTIONAL MATCH (plan)-[:CUSTOMIZED_AS]->(policy:Employer_Group_Policy)
                      -[:HAS_OVERRIDE]->(override:Benefit_Override)
                WHERE $employer_policy_id IS NOT NULL
                  AND toUpper(policy.policy_id) = $employer_policy_id
                  AND toUpper(override.dental_code) = $procedure_code
                  AND toUpper(override.code_system) = $procedure_system
                RETURN properties(plan) AS plan, properties(benefit) AS benefit,
                       properties(procedure) AS procedure, properties(policy) AS employer_policy,
                       properties(override) AS benefit_override
                LIMIT 2
                """,
                payer_ids=_identifier_candidates(payer),
                plan_id=plan,
                procedure_code=procedure,
                procedure_system=system,
                service_date=_date_text(service_date),
                employer_policy_id=normalize_code(employer_policy_id) or None,
            )
        except KnowledgeGraphUnavailable as exc:
            return KnowledgeResult(status=KnowledgeStatus.UNAVAILABLE, reason=str(exc), **base)
        if not rows:
            return KnowledgeResult(
                status=KnowledgeStatus.UNKNOWN,
                reason="No matching Plan_Benefit was found; absence is not proof of exclusion.",
                **base,
            )
        row = rows[0]
        benefit = {**(row.get("benefit") or {}), **(row.get("benefit_override") or {})}
        covered = benefit.get("covered")
        waiting_days = int(benefit.get("waiting_period_days") or 0)
        if covered is False:
            status = KnowledgeStatus.NOT_SUPPORTED
        elif covered is True and waiting_days > 0:
            status = KnowledgeStatus.CONDITIONAL
        elif covered is True:
            status = KnowledgeStatus.SUPPORTED
        else:
            status = KnowledgeStatus.UNKNOWN
        return KnowledgeResult(
            status=status,
            entity_id=benefit.get("override_id") or benefit.get("benefit_id"),
            reason="Plan benefit and applicable employer override were evaluated.",
            evidence=row,
            **base,
        )

    def query_prior_authorization(
        self, *, payer_id: str, plan_id: str, procedure_code: str,
        procedure_system: str, service_date: str | None = None,
    ) -> KnowledgeResult:
        payer = normalize_code(payer_id)
        plan = normalize_code(plan_id)
        procedure = normalize_code(procedure_code)
        system = normalize_code_system(procedure_system)
        base = dict(
            query="prior_authorization", source="NEO4J", payer_id=payer, plan_id=plan,
            procedure_code=procedure, procedure_system=system,
        )
        benefit = self.query_plan_benefit(
            payer_id=payer, plan_id=plan, procedure_code=procedure,
            procedure_system=system, service_date=service_date,
        )
        if benefit.status == KnowledgeStatus.UNAVAILABLE:
            return KnowledgeResult(status=KnowledgeStatus.UNAVAILABLE, reason=benefit.reason, **base)
        try:
            rules = self._read(
                """
                MATCH (rule:Prior_Authorization_Rule)-[:GOVERNS_PA]->(procedure:Dental_Procedure_Code)
                WHERE toUpper(procedure.code) = $procedure_code
                  AND toUpper(procedure.code_system) = $procedure_system
                  AND toUpper(rule.procedure_code) = $procedure_code
                  AND toUpper(rule.code_system) = $procedure_system
                  AND toUpper(rule.payer_id) IN $payer_ids
                  AND ($service_date IS NULL OR rule.effective_date IS NULL
                       OR date(rule.effective_date) <= date($service_date))
                  AND (coalesce(rule.applies_to_all_plans, false) = true OR EXISTS {
                      MATCH (rule)-[:APPLIES_TO_PLAN]->(applicable_plan:Insurance_Plan)
                      WHERE toUpper(applicable_plan.plan_id) = $plan_id
                  })
                RETURN properties(rule) AS rule
                ORDER BY rule.effective_date DESC, rule.last_verified_at DESC
                """,
                payer_ids=_identifier_candidates(payer),
                plan_id=plan,
                procedure_code=procedure,
                procedure_system=system,
                service_date=_date_text(service_date),
            )
        except KnowledgeGraphUnavailable as exc:
            return KnowledgeResult(status=KnowledgeStatus.UNAVAILABLE, reason=str(exc), **base)
        decisions = {bool(row["rule"].get("pa_required")) for row in rules if row.get("rule")}
        benefit_value = (benefit.evidence.get("benefit") or {}).get("prior_auth_required")
        if benefit_value is not None:
            decisions.add(bool(benefit_value))
        if not decisions:
            status = KnowledgeStatus.UNKNOWN
        elif len(decisions) > 1:
            status = KnowledgeStatus.CONFLICT
        else:
            status = KnowledgeStatus.REQUIRED if True in decisions else KnowledgeStatus.NOT_REQUIRED
        evidence = {"rules": [row["rule"] for row in rules], "plan_benefit": benefit.to_dict()}
        entity_id = next((row["rule"].get("rule_id") for row in rules if row.get("rule")), benefit.entity_id)
        reason = "Conflicting Plan_Benefit and PA rule decisions require review." if status == KnowledgeStatus.CONFLICT else None
        return KnowledgeResult(status=status, entity_id=entity_id, reason=reason, evidence=evidence, **base)

    def query_bundling(
        self, *, procedure_code: str, procedure_system: str,
        payer_id: str | None = None, plan_id: str | None = None,
        service_date: str | None = None,
    ) -> KnowledgeResult:
        procedure = normalize_code(procedure_code)
        system = normalize_code_system(procedure_system)
        base = dict(query="bundling", source="NEO4J", procedure_code=procedure, procedure_system=system)
        try:
            rows = self._read(
                """
                MATCH (procedure:Dental_Procedure_Code)-[relationship:CANNOT_BILL_WITH]
                      ->(other:Dental_Procedure_Code)
                WHERE toUpper(procedure.code) = $procedure_code
                  AND toUpper(procedure.code_system) = $procedure_system
                RETURN other.code AS bundled_code, properties(relationship) AS relationship,
                       NULL AS rule
                UNION
                MATCH (procedure:Dental_Procedure_Code)-[:HAS_BUNDLING_RULE]->(rule:Bundling_Rule)
                      -[:CANNOT_BILL_WITH]->(other:Dental_Procedure_Code)
                WHERE toUpper(procedure.code) = $procedure_code
                  AND toUpper(procedure.code_system) = $procedure_system
                RETURN other.code AS bundled_code, {} AS relationship, properties(rule) AS rule
                """,
                procedure_code=procedure,
                procedure_system=system,
            )
        except KnowledgeGraphUnavailable as exc:
            return KnowledgeResult(status=KnowledgeStatus.UNAVAILABLE, reason=str(exc), **base)
        bundled = sorted({normalize_code(row.get("bundled_code")) for row in rows if row.get("bundled_code")})
        return KnowledgeResult(
            status=KnowledgeStatus.SUPPORTED if bundled else KnowledgeStatus.UNKNOWN,
            evidence={"bundled_codes": bundled, "rules": rows},
            reason=None if bundled else "No applicable bundling rule was found.",
            **base,
        )

    def query_documentation_requirements(
        self, *, procedure_code: str, procedure_system: str,
    ) -> KnowledgeResult:
        procedure = normalize_code(procedure_code)
        system = normalize_code_system(procedure_system)
        base = dict(
            query="documentation_requirements", source="NEO4J",
            procedure_code=procedure, procedure_system=system,
        )
        try:
            rows = self._read(
                """
                MATCH (procedure:Dental_Procedure_Code)
                WHERE toUpper(procedure.code) = $procedure_code
                  AND toUpper(procedure.code_system) = $procedure_system
                RETURN procedure.code AS code, procedure.required_docs AS required_docs,
                       procedure.required_docs_for_submission AS required_docs_for_submission,
                       procedure.version AS version
                LIMIT 1
                """,
                procedure_code=procedure,
                procedure_system=system,
            )
        except KnowledgeGraphUnavailable as exc:
            return KnowledgeResult(status=KnowledgeStatus.UNAVAILABLE, reason=str(exc), **base)
        if not rows:
            return KnowledgeResult(status=KnowledgeStatus.UNKNOWN, reason="Procedure is not represented in the KG.", **base)
        documents = sorted(
            set(_documents(rows[0].get("required_docs")))
            | set(_documents(rows[0].get("required_docs_for_submission")))
        )
        return KnowledgeResult(
            status=KnowledgeStatus.REQUIRED if documents else KnowledgeStatus.NOT_REQUIRED,
            evidence={"required_documents": documents, "procedure": rows[0]},
            **base,
        )

    def _read(self, query: str, **parameters: Any) -> list[dict[str, Any]]:
        try:
            statement = self._query_type(query, timeout=self.query_timeout_seconds)
            with self._driver.session(database=self.database, default_access_mode=self._read_access) as session:
                rows = [record.data() for record in session.run(statement, parameters)]
            self._last_error = None
            return rows
        except Exception as exc:
            self._last_error = _safe_error(exc)
            logger.error("Neo4j read query failed: %s", self._last_error)
            raise KnowledgeGraphUnavailable(self._last_error) from exc


def _documents(value: Any) -> list[str]:
    values = value if isinstance(value, list) else re.split(r"[,;]", str(value or ""))
    return sorted({str(item).strip() for item in values if str(item).strip().lower() not in {"", "nan", "none"}})


def _date_text(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else None


def _identifier_candidates(value: str) -> list[str]:
    normalized = normalize_code(value)
    values = {normalized, re.sub(r"[^A-Z0-9]", "", normalized)}
    for suffix in ("_AE", "-AE", " UAE", "_SA", "-SA", " KSA"):
        if normalized.endswith(suffix):
            base = normalized[: -len(suffix)]
            values.update({base, re.sub(r"[^A-Z0-9]", "", base)})
    return sorted(item for item in values if item)


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"
