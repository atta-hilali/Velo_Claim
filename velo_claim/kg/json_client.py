from __future__ import annotations

import json
from pathlib import Path

from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.models import KnowledgeResult, KnowledgeStatus, normalize_code_system


class JsonKnowledgeGraphClient(Neo4jClientInterface):
    """Read-only, deterministic snapshot adapter for development and tests."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"Knowledge graph snapshot not found: {self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Knowledge graph snapshot must be a JSON object.")
        self.version = str(raw.get("version") or "unknown")
        self._data = raw
        self._edges = [item for item in raw.get("edges", []) if isinstance(item, dict)]

    def query_diagnosis_procedure_compatibility(
        self, *, diagnosis_code: str, procedure_code: str, diagnosis_system: str,
        procedure_system: str, service_date: str | None = None,
    ) -> KnowledgeResult:
        diagnosis = normalize_code(diagnosis_code)
        procedure = normalize_code(procedure_code)
        procedure_system = normalize_code_system(procedure_system)
        matching = [
            edge for edge in self._edges
            if normalize_code(edge.get("diagnosis_code")) == diagnosis
            and normalize_code(edge.get("procedure_code")) == procedure
            and normalize_code_system(edge.get("procedure_system")) in {"UNKNOWN", procedure_system}
        ]
        supported = any(
            str(edge.get("relationship") or "").lower()
            in {"supports", "justifies", "compatible", "compatible_with"}
            for edge in matching
        )
        return KnowledgeResult(
            status=KnowledgeStatus.SUPPORTED if supported else KnowledgeStatus.UNKNOWN,
            query="diagnosis_procedure_compatibility", source="JSON",
            entity_id=matching[0].get("id") if matching else None,
            diagnosis_code=diagnosis,
            diagnosis_system=normalize_code_system(diagnosis_system, diagnosis=True),
            procedure_code=procedure, procedure_system=procedure_system,
            reason=matching[0].get("evidence") if matching else None,
            evidence={"edges": matching, "snapshot_version": self.version},
        )

    def query_prior_authorization(
        self, *, payer_id: str, plan_id: str, procedure_code: str,
        procedure_system: str, service_date: str | None = None,
    ) -> KnowledgeResult:
        payer, plan, code = normalize_code(payer_id), normalize_code(plan_id), normalize_code(procedure_code)
        system = normalize_code_system(procedure_system)
        matches = []
        for rule in self._data.get("prior_authorization", []):
            if not isinstance(rule, dict) or normalize_code(rule.get("procedure_code")) != code:
                continue
            if normalize_code_system(rule.get("code_system")) not in {"UNKNOWN", system}:
                continue
            rule_payer, rule_plan = normalize_code(rule.get("payer_id")), normalize_code(rule.get("plan_id"))
            if rule_payer not in {"", "*", payer} or rule_plan not in {"", "*", plan}:
                continue
            matches.append(rule)
        decisions = {bool(rule.get("required", True)) for rule in matches}
        status = KnowledgeStatus.UNKNOWN
        if decisions:
            status = KnowledgeStatus.CONFLICT if len(decisions) > 1 else (
                KnowledgeStatus.REQUIRED if True in decisions else KnowledgeStatus.NOT_REQUIRED
            )
        return KnowledgeResult(
            status=status, query="prior_authorization", source="JSON", payer_id=payer,
            plan_id=plan, procedure_code=code, procedure_system=system,
            evidence={"rules": matches, "snapshot_version": self.version},
        )

    def query_plan_benefit(
        self, *, payer_id: str, plan_id: str, procedure_code: str,
        procedure_system: str, service_date: str | None = None,
        employer_policy_id: str | None = None,
    ) -> KnowledgeResult:
        matches = [
            item for item in self._data.get("plan_benefits", [])
            if isinstance(item, dict)
            and normalize_code(item.get("payer_id")) == normalize_code(payer_id)
            and normalize_code(item.get("plan_id")) == normalize_code(plan_id)
            and normalize_code(item.get("procedure_code") or item.get("dental_code")) == normalize_code(procedure_code)
            and normalize_code_system(item.get("code_system")) == normalize_code_system(procedure_system)
        ]
        benefit = matches[0] if matches else None
        status = KnowledgeStatus.UNKNOWN
        if benefit:
            status = KnowledgeStatus.SUPPORTED if benefit.get("covered") is True else KnowledgeStatus.NOT_SUPPORTED
            if status == KnowledgeStatus.SUPPORTED and int(benefit.get("waiting_period_days") or 0) > 0:
                status = KnowledgeStatus.CONDITIONAL
        return KnowledgeResult(
            status=status, query="plan_benefit", source="JSON",
            entity_id=benefit.get("benefit_id") if benefit else None,
            payer_id=normalize_code(payer_id), plan_id=normalize_code(plan_id),
            procedure_code=normalize_code(procedure_code), procedure_system=normalize_code_system(procedure_system),
            evidence={"benefit": benefit, "snapshot_version": self.version},
        )

    def query_bundling(
        self, *, procedure_code: str, procedure_system: str,
        payer_id: str | None = None, plan_id: str | None = None,
        service_date: str | None = None,
    ) -> KnowledgeResult:
        code = normalize_code(procedure_code)
        rules = [rule for rule in self._data.get("bundling_rules", [])
                 if isinstance(rule, dict) and normalize_code(rule.get("procedure_code")) == code]
        bundled = sorted({normalize_code(item) for rule in rules for item in rule.get("bundled_codes", []) if item})
        return KnowledgeResult(
            status=KnowledgeStatus.SUPPORTED if rules else KnowledgeStatus.UNKNOWN,
            query="bundling", source="JSON", procedure_code=code,
            procedure_system=normalize_code_system(procedure_system),
            evidence={"bundled_codes": bundled, "rules": rules, "snapshot_version": self.version},
        )

    def query_documentation_requirements(
        self, *, procedure_code: str, procedure_system: str,
    ) -> KnowledgeResult:
        code = normalize_code(procedure_code)
        rules = [rule for rule in self._data.get("documentation_rules", [])
                 if isinstance(rule, dict) and normalize_code(rule.get("procedure_code")) == code]
        documents = sorted({normalize_code(item) for rule in rules
                            for item in rule.get("required_documents", []) if item})
        return KnowledgeResult(
            status=KnowledgeStatus.REQUIRED if documents else KnowledgeStatus.UNKNOWN,
            query="documentation_requirements", source="JSON", procedure_code=code,
            procedure_system=normalize_code_system(procedure_system),
            evidence={"required_documents": documents, "rules": rules, "snapshot_version": self.version},
        )

    def diagnostics(self) -> dict:
        return {"backend": "json", "connectivity": "healthy", "database": None,
                "snapshot_version": self.version, "mock_fallback": False, "production_safe": False}
