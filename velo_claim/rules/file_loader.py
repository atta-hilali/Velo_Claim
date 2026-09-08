from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from velo_claim.core.models import PayerRuleSet
from velo_claim.core.utils import normalize_code
from velo_claim.rules.interface import PayerRuleLoaderInterface


class FilePayerRuleLoader(PayerRuleLoaderInterface):
    """Load active payer rules from the versioned rule registry."""

    def __init__(self, path: str | Path, payer_registry_path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"Payer rule registry not found: {self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._rules = raw.get("rules", []) if isinstance(raw, dict) else raw
        if not isinstance(self._rules, list):
            raise ValueError("Payer rule registry must contain a rules array.")
        self.version = str(raw.get("_meta", {}).get("version") or "unknown") if isinstance(raw, dict) else "unknown"
        self._aliases = self._load_aliases(payer_registry_path)

    def _load_aliases(self, path: str | Path | None) -> dict[str, str]:
        if not path:
            return {}
        registry_path = Path(path).expanduser().resolve()
        if not registry_path.is_file():
            return {}
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
        aliases: dict[str, str] = {}
        for payer in raw.get("payers", []):
            canonical = _token(payer.get("canonical_id"))
            for value in [payer.get("canonical_id"), payer.get("display_name"), *payer.get("aliases", [])]:
                if value:
                    aliases[_token(value)] = canonical
        return aliases

    def load(self, payer_id: str, plan_id: str) -> PayerRuleSet:
        payer = _token(payer_id) or "unknown"
        canonical = self._aliases.get(payer, payer)
        today = date.today()
        selected = [
            dict(rule)
            for rule in self._rules
            if isinstance(rule, dict)
            and str(rule.get("status") or "ACTIVE").upper() == "ACTIVE"
            and self._matches_payer(rule, payer, canonical)
            and _is_effective(rule, today)
            and self._matches_plan(rule, plan_id)
        ]

        pa_codes: set[str] = set()
        bundling: dict[str, set[str]] = {}
        required_docs: dict[str, set[str]] = {}
        layer_caps: dict[str, float] = {}
        eligibility_ttl = 3600
        submission_channel = "MANUAL_PORTAL"

        for rule in selected:
            rule_type = str(rule.get("rule_type") or "").upper()
            condition = rule.get("condition") or {}
            action = rule.get("action") or {}
            codes = _rule_codes(condition, rule)
            if rule_type in {"PRIOR_AUTH", "PRIOR_AUTHORIZATION"}:
                pa_codes.update(codes)
            if rule_type in {"BUNDLING", "BUNDLED_PROCEDURES"}:
                for code in codes:
                    bundling.setdefault(code, set()).update(
                        normalize_code(item)
                        for item in action.get("bundled_codes", condition.get("bundled_codes", []))
                        if item
                    )
            documents = action.get("required_documents") or rule.get("required_documents") or []
            for code in codes:
                required_docs.setdefault(code, set()).update(normalize_code(item) for item in documents if item)
            if rule_type == "ELIGIBILITY" and action.get("ttl_seconds"):
                eligibility_ttl = int(action["ttl_seconds"])
            if action.get("submission_channel"):
                submission_channel = str(action["submission_channel"])
            cap = rule.get("max_deduction_per_layer")
            if cap is not None:
                layer = _layer_for_rule(rule_type)
                layer_caps[layer] = max(layer_caps.get(layer, 0.0), float(cap))

        return PayerRuleSet(
            payer_id=payer_id or "UNKNOWN",
            plan_id=plan_id or "UNKNOWN",
            eligibility_ttl_seconds=eligibility_ttl,
            pa_required_cpt_codes=sorted(pa_codes),
            bundling_rules={key: sorted(value) for key, value in bundling.items()},
            required_doc_types={key: sorted(value) for key, value in required_docs.items()},
            submission_channel=submission_channel,
            rules=selected,
            max_deduction_per_layer=layer_caps,
            source_version=self.version,
            source="FILE",
        )

    def _matches_payer(self, rule: dict[str, Any], payer: str, canonical: str) -> bool:
        rule_payer = _token(rule.get("payer_id"))
        if rule_payer in {"", "*", "default"}:
            return True
        rule_canonical = self._aliases.get(rule_payer, rule_payer)
        return rule_payer in {payer, canonical} or rule_canonical == canonical

    @staticmethod
    def _matches_plan(rule: dict[str, Any], plan_id: str) -> bool:
        configured = _token(rule.get("plan_id") or (rule.get("condition") or {}).get("plan_id"))
        return configured in {"", "*", _token(plan_id)}


def _token(value: Any) -> str:
    return "".join(character for character in str(value or "").strip().lower() if character.isalnum() or character == "_")


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _is_effective(rule: dict[str, Any], current: date) -> bool:
    start = _as_date(rule.get("effective_from"))
    end = _as_date(rule.get("effective_to"))
    return not ((start and current < start) or (end and current > end))


def _rule_codes(condition: dict[str, Any], rule: dict[str, Any]) -> set[str]:
    raw: list[Any] = []
    for value in (
        condition.get("cpt"),
        condition.get("code"),
        condition.get("value"),
        condition.get("cpt_codes"),
        condition.get("procedure_codes"),
        rule.get("cpt_codes"),
        rule.get("procedure_codes"),
    ):
        raw.extend(value if isinstance(value, list) else [value])
    return {normalize_code(item) for item in raw if item and not isinstance(item, dict)}


def _layer_for_rule(rule_type: str) -> str:
    if rule_type in {"PRIOR_AUTH", "PRIOR_AUTHORIZATION"}:
        return "PRIOR_AUTHORIZATION"
    if rule_type in {"DOCUMENTATION"}:
        return "DOCUMENTATION"
    if rule_type in {"ELIGIBILITY"}:
        return "ELIGIBILITY"
    if rule_type in {"TIMELY_FILING"}:
        return "TIMELY_FILING"
    if rule_type in {"CPT_REQUIRES_ICD", "CODING", "BUNDLING", "MAX_UNITS"}:
        return "CODING"
    return "PAYER_RULES"
