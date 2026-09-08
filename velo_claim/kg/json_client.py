from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface


class JsonKnowledgeGraphClient(Neo4jClientInterface):
    """Read-only KG adapter for a versioned JSON snapshot.

    This is a deterministic production fallback when Neo4j is unavailable. It
    is intentionally distinct from the static mock client and exposes the same
    contract as the live graph.
    """

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

    def query_icd_cpt_compatibility(self, icd_code: str, cpt_code: str) -> bool | None:
        icd = normalize_code(icd_code)
        procedure = normalize_code(cpt_code)
        matching = [
            edge
            for edge in self._edges
            if normalize_code(edge.get("diagnosis_code")) == icd
            and normalize_code(edge.get("procedure_code")) == procedure
        ]
        if not matching:
            return None
        return any(
            str(edge.get("relationship") or "").lower()
            in {"supports", "justifies", "compatible", "compatible_with"}
            for edge in matching
        )

    def query_pa_required(self, payer_id: str, plan_id: str, cpt_code: str) -> bool:
        payer = normalize_code(payer_id)
        plan = normalize_code(plan_id)
        code = normalize_code(cpt_code)
        for rule in self._data.get("prior_authorization", []):
            if not isinstance(rule, dict):
                continue
            if normalize_code(rule.get("procedure_code")) != code:
                continue
            rule_payer = normalize_code(rule.get("payer_id"))
            rule_plan = normalize_code(rule.get("plan_id"))
            if rule_payer not in {"", "*", payer} or rule_plan not in {"", "*", plan}:
                continue
            return bool(rule.get("required", True))
        return False

    def query_bundled_procedures(self, cpt_code: str) -> list[str]:
        code = normalize_code(cpt_code)
        result: set[str] = set()
        for rule in self._data.get("bundling_rules", []):
            if not isinstance(rule, dict) or normalize_code(rule.get("procedure_code")) != code:
                continue
            result.update(normalize_code(item) for item in rule.get("bundled_codes", []) if item)
        return sorted(result)

    def query_required_documents(self, cpt_code: str) -> list[str]:
        code = normalize_code(cpt_code)
        result: set[str] = set()
        for rule in self._data.get("documentation_rules", []):
            if not isinstance(rule, dict) or normalize_code(rule.get("procedure_code")) != code:
                continue
            result.update(normalize_code(item) for item in rule.get("required_documents", []) if item)
        return sorted(result)
