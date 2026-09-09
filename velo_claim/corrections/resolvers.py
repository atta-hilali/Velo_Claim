from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from velo_claim.corrections.patches import (
    UnsafeCorrectionError,
    get_canonical_value,
    validate_correction_path,
    validate_proposed_value,
    values_equal,
)
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.storage.interfaces import RepositoryInterface


CORE_RULE_VERSION = "1.0"


class CorrectionLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    can_suggest: bool
    field_path: str
    current_value: Any = None
    proposed_value: Any = None
    rationale: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)
    rule_refs: list[dict[str, Any]] = Field(default_factory=list)
    requires_manual_reconciliation: bool = False


@dataclass(slots=True)
class CorrectionLLMClient:
    enabled: bool
    base_url: str
    api_key: str
    model: str
    api_style: str
    generate_path: str
    timeout_seconds: int
    minimum_confidence: float

    @classmethod
    def from_env(cls) -> "CorrectionLLMClient":
        enabled = _env_bool(
            "USE_CORRECTION_LLM",
            _env_bool("USE_VALIDATION_LLM", _env_bool("USE_MEDGEMMA", False)),
        )
        return cls(
            enabled=enabled,
            base_url=(
                os.getenv("CORRECTION_LLM_BASE_URL")
                or os.getenv("VALIDATION_LLM_BASE_URL")
                or os.getenv("MEDGEMMA_BASE_URL")
                or ""
            ).rstrip("/"),
            api_key=(
                os.getenv("CORRECTION_LLM_API_KEY")
                or os.getenv("VALIDATION_LLM_API_KEY")
                or os.getenv("MEDGEMMA_API_KEY")
                or ""
            ),
            model=(
                os.getenv("CORRECTION_LLM_MODEL")
                or os.getenv("VALIDATION_LLM_MODEL")
                or os.getenv("MEDGEMMA_MODEL")
                or "medgemma"
            ),
            api_style=(
                os.getenv("CORRECTION_LLM_API_STYLE")
                or os.getenv("VALIDATION_LLM_API_STYLE")
                or os.getenv("MEDGEMMA_API_STYLE")
                or "openai_chat"
            ),
            generate_path=(
                os.getenv("CORRECTION_LLM_GENERATE_PATH")
                or os.getenv("VALIDATION_LLM_GENERATE_PATH")
                or os.getenv("MEDGEMMA_GENERATE_PATH")
                or "/generate"
            ),
            timeout_seconds=int(os.getenv("CORRECTION_LLM_TIMEOUT_SECONDS", "30")),
            minimum_confidence=float(os.getenv("CORRECTION_LLM_MIN_CONFIDENCE", "0.80")),
        )

    def resolve(self, context: dict[str, Any]) -> CorrectionLLMResponse:
        if not self.enabled or not self.base_url:
            raise RuntimeError("Correction LLM is disabled or not configured.")
        prompt = json.dumps(context, sort_keys=True, default=str)
        system = (
            "You propose evidence-grounded claim corrections. Return strict JSON only. "
            "Never invent identity, diagnoses, procedures, documents, payer decisions, or external references. "
            "If evidence is insufficient, set can_suggest=false and requires_manual_reconciliation=true."
        )
        if self.api_style == "openai_chat":
            body = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            endpoint = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        else:
            body = {"model": self.model, "prompt": system + "\n" + prompt, "temperature": 0}
            endpoint = self.base_url + "/" + self.generate_path.strip("/")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if self.api_style == "openai_chat":
            content = payload.get("choices", [{}])[0].get("message", {}).get("content")
        else:
            content = payload.get("generated_text") or payload.get("text") or payload.get("response")
        if isinstance(content, dict):
            decoded = content
        else:
            decoded = json.loads(_strip_json_fence(str(content or "")))
        return CorrectionLLMResponse.model_validate(decoded)


def deterministic_candidate(
    *,
    group: dict[str, Any],
    canonical_claim: dict[str, Any],
    state: dict[str, Any],
    repository: RepositoryInterface,
) -> dict[str, Any] | None:
    if group.get("manual_reason"):
        return None
    field_path = group["field_path"]
    try:
        current = get_canonical_value(canonical_claim, field_path)
        validate_correction_path(field_path)
    except UnsafeCorrectionError:
        return None

    codes = set(group.get("issue_codes") or [])
    proposed: Any = None
    rule_ref: dict[str, Any] | None = None
    if "FINANCIAL_GROSS_MISMATCH" in codes:
        proposed = _line_total(canonical_claim, "gross")
        rule_ref = _core_rule("CORE_FINANCIAL_GROSS_FROM_LINES")
    elif "FINANCIAL_NET_MISMATCH" in codes:
        proposed = _line_total(canonical_claim, "net")
        rule_ref = _core_rule("CORE_FINANCIAL_NET_FROM_LINES")
    elif "FINANCIAL_PATIENT_SHARE_MISMATCH" in codes:
        proposed = _line_total(canonical_claim, "patient_share")
        rule_ref = _core_rule("CORE_FINANCIAL_PATIENT_SHARE_FROM_LINES")
    elif "CURRENCY_MISMATCH" in codes:
        proposed = state.get("routing_context", {}).get("currency")
        rule_ref = _core_rule("CORE_CURRENCY_FROM_VERIFIED_ROUTE")

    db_rule = None
    if proposed is None:
        for issue in group.get("issues", []):
            db_rule = repository.get_approved_correction_rule(issue["code"], issue["check_type"], field_path)
            if db_rule:
                break
        if db_rule:
            proposed = _evaluate_rule_action(db_rule.get("action") or {}, canonical_claim, state)
            rule_ref = {
                "rule_key": db_rule.get("rule_key"),
                "version": db_rule.get("version"),
                "approved_by": db_rule.get("approved_by"),
                "status": db_rule.get("status"),
            }

    if proposed is None or rule_ref is None:
        return None
    try:
        validate_proposed_value(field_path, current, proposed)
    except UnsafeCorrectionError:
        return None
    return {
        "field_path": field_path,
        "old_value": current,
        "proposed_value": proposed,
        "source": "RULE_ENGINE",
        "confidence": 1.0,
        "rationale": f"Applied approved deterministic rule {rule_ref['rule_key']}.",
        "evidence": {"line_items": canonical_claim.get("line_items", []), "route_currency": state.get("routing_context", {}).get("currency")},
        "rule_refs": [rule_ref],
        "issue_ids": group.get("issue_ids", []),
        "issue_codes": group.get("issue_codes", []),
    }


def kg_evidence_for_group(
    group: dict[str, Any], canonical_claim: dict[str, Any], kg_client: Neo4jClientInterface
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    try:
        if group.get("issues", [{}])[0].get("check_type") == "CODING":
            procedure = _collection_item(group["field_path"], canonical_claim, "procedures")
            if procedure and procedure.get("code"):
                evidence["diagnosis_procedure"] = [
                    kg_client.query_diagnosis_procedure_compatibility(
                        diagnosis_code=str(diagnosis.get("code") or ""),
                        procedure_code=str(procedure.get("code") or ""),
                        diagnosis_system=str(diagnosis.get("system") or "ICD-10"),
                        procedure_system=str(procedure.get("system") or "CPT"),
                        service_date=canonical_claim.get("encounter", {}).get("service_date"),
                    ).to_dict()
                    for diagnosis in canonical_claim.get("diagnoses", [])
                    if diagnosis.get("code")
                ]
        if group.get("issues", [{}])[0].get("check_type") == "DOCUMENTATION":
            evidence["documentation_requirements"] = [
                kg_client.query_documentation_requirements(
                    procedure_code=str(item.get("code") or ""),
                    procedure_system=str(item.get("system") or "CPT"),
                ).to_dict()
                for item in canonical_claim.get("line_items", [])
                if item.get("code")
            ]
    except Exception as exc:
        evidence["unavailable"] = {"reason": type(exc).__name__}
    return evidence


def llm_candidate(
    *,
    group: dict[str, Any],
    canonical_claim: dict[str, Any],
    source_context: dict[str, Any],
    route: dict[str, Any],
    payer_rule_set: dict[str, Any],
    kg_evidence: dict[str, Any],
    client: CorrectionLLMClient,
) -> dict[str, Any] | None:
    if group.get("manual_reason"):
        return None
    try:
        current = get_canonical_value(canonical_claim, group["field_path"])
        validate_correction_path(group["field_path"])
    except UnsafeCorrectionError:
        return None
    source_candidates = _relevant_source_candidates(group["field_path"], source_context)
    evidence_refs = [f"issue:{item}" for item in group.get("issue_ids", [])]
    evidence_refs.extend(f"source:{item['path']}" for item in source_candidates)
    if kg_evidence:
        evidence_refs.append("kg:query_result")
    relevant_payer_rules = _relevant_payer_rules(group, payer_rule_set)
    context = {
        "issue_group": {
            "field_path": group["field_path"],
            "issues": [
                {key: issue.get(key) for key in ("issue_id", "code", "check_type", "severity", "message", "suggestion")}
                for issue in group.get("issues", [])
            ],
        },
        "current_value": current,
        "source_candidates": source_candidates,
        "kg_evidence": kg_evidence,
        "payer_rules": relevant_payer_rules,
        "route": {key: route.get(key) for key in ("jurisdiction", "claim_standard", "payer_rule_profile")},
        "allowed_evidence_refs": evidence_refs,
        "required_output_schema": CorrectionLLMResponse.model_json_schema(),
    }
    try:
        result = client.resolve(context)
        if result.requires_manual_reconciliation or not result.can_suggest:
            return None
        if result.field_path != group["field_path"] or not values_equal(result.current_value, current):
            return None
        if result.confidence < client.minimum_confidence or not result.evidence_refs:
            return None
        if not set(result.evidence_refs).issubset(set(evidence_refs)):
            return None
        if result.rule_refs and not all(
            any(_rule_ref_matches(reference, rule) for rule in relevant_payer_rules)
            for reference in result.rule_refs
        ):
            return None
        validate_proposed_value(result.field_path, current, result.proposed_value)
        if _requires_source_value(result.field_path) and not any(
            values_equal(result.proposed_value, candidate["value"]) for candidate in source_candidates
        ):
            return None
    except Exception:
        return None
    return {
        "field_path": result.field_path,
        "old_value": current,
        "proposed_value": result.proposed_value,
        "source": "LLM",
        "confidence": result.confidence,
        "rationale": result.rationale,
        "evidence": {
            "evidence_refs": result.evidence_refs,
            "source_candidates": source_candidates,
            "kg": kg_evidence,
        },
        "rule_refs": result.rule_refs,
        "issue_ids": group.get("issue_ids", []),
        "issue_codes": group.get("issue_codes", []),
    }


def manual_candidate(group: dict[str, Any], canonical_claim: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    try:
        old_value = get_canonical_value(canonical_claim, group["field_path"])
    except UnsafeCorrectionError:
        old_value = None
    return {
        "field_path": group["field_path"],
        "old_value": old_value,
        "proposed_value": None,
        "source": "MANUAL_REQUIRED",
        "confidence": 0.0,
        "rationale": reason or group.get("manual_reason") or "No evidence-backed safe correction could be produced.",
        "evidence": {"issues": group.get("issues", []), "history": group.get("history", [])},
        "rule_refs": [],
        "issue_ids": group.get("issue_ids", []),
        "issue_codes": group.get("issue_codes", []),
    }


def merge_candidates(candidates: list[dict[str, Any]], canonical_claim: dict[str, Any]) -> list[dict[str, Any]]:
    by_path: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_path.setdefault(candidate["field_path"], []).append(candidate)
    merged = []
    for field_path, entries in by_path.items():
        non_manual = [item for item in entries if item.get("source") != "MANUAL_REQUIRED"]
        values: list[Any] = []
        for item in non_manual:
            if not any(values_equal(item.get("proposed_value"), value) for value in values):
                values.append(item.get("proposed_value"))
        if not non_manual:
            merged.append(entries[0])
            continue
        deterministic = [item for item in non_manual if item.get("source") == "RULE_ENGINE"]
        if deterministic:
            selected = deterministic[0]
            if any(not values_equal(item.get("proposed_value"), selected.get("proposed_value")) for item in deterministic[1:]):
                merged.append(manual_candidate(_group_from_entries(entries), canonical_claim, "Approved deterministic rules conflict for this field."))
                continue
        elif len(values) > 1:
            merged.append(manual_candidate(_group_from_entries(entries), canonical_claim, "Evidence-backed candidates conflict for this field."))
            continue
        else:
            selected = max(non_manual, key=lambda item: float(item.get("confidence") or 0))
        compatible = [item for item in entries if values_equal(item.get("proposed_value"), selected.get("proposed_value"))]
        combined = dict(selected)
        combined["issue_ids"] = sorted({value for item in compatible for value in item.get("issue_ids", [])})
        combined["issue_codes"] = sorted({value for item in compatible for value in item.get("issue_codes", [])})
        combined["rule_refs"] = [value for item in compatible for value in item.get("rule_refs", [])]
        combined["evidence"] = {"sources": [item.get("evidence", {}) for item in compatible]}
        combined["source"] = selected["source"] if len({item["source"] for item in compatible}) == 1 else "MIXED"
        combined["rationale"] = " ".join(dict.fromkeys(item["rationale"] for item in compatible))
        merged.append(combined)
    return merged


def _core_rule(rule_key: str) -> dict[str, Any]:
    return {"rule_key": rule_key, "version": CORE_RULE_VERSION, "approved_by": "VELO_CLAIM_CORE", "status": "ACTIVE"}


def _line_total(claim: dict[str, Any], field: str) -> float:
    return round(sum(float(item.get(field) or 0) for item in claim.get("line_items", [])), 2)


def _evaluate_rule_action(action: dict[str, Any], claim: dict[str, Any], state: dict[str, Any]) -> Any:
    if "value" in action:
        return action["value"]
    if action.get("calculation") == "sum_line_field":
        return _line_total(claim, str(action.get("line_field") or ""))
    if action.get("source_path"):
        return _read_dotted({"canonical_claim": claim, **state}, str(action["source_path"]))
    return None


def _read_dotted(value: Any, path: str) -> Any:
    for token in path.split("."):
        if not isinstance(value, dict) or token not in value:
            return None
        value = value[token]
    return value


def _collection_item(path: str, claim: dict[str, Any], collection: str) -> dict[str, Any] | None:
    import re

    match = re.search(rf"canonical_claim\.{collection}\[(\d+)\]", path)
    if not match:
        return None
    items = claim.get(collection, [])
    index = int(match.group(1))
    return items[index] if isinstance(items, list) and index < len(items) else None


def _relevant_source_candidates(field_path: str, source_context: dict[str, Any]) -> list[dict[str, Any]]:
    leaf = field_path.rsplit(".", 1)[-1]
    if "]" in leaf:
        leaf = ""
    found: list[dict[str, Any]] = []

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > 6 or len(found) >= 30:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                next_path = f"{path}.{key}" if path else key
                if key == leaf and item is not None and not isinstance(item, (dict, list)):
                    found.append({"path": next_path, "value": item})
                walk(item, next_path, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                walk(item, f"{path}[{index}]", depth + 1)

    walk(source_context, "source_context", 0)
    return found


def _relevant_payer_rules(group: dict[str, Any], payer_rule_set: dict[str, Any]) -> list[dict[str, Any]]:
    identifiers = {str(value).lower() for value in group.get("issue_codes") or [] if value}
    for issue in group.get("issues", []):
        identifiers.update(_rule_identifiers(issue.get("evidence") or {}))
    return [
        rule
        for rule in payer_rule_set.get("rules", [])
        if identifiers.intersection(_rule_identifiers(rule))
    ][:10]


def _rule_identifiers(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"id", "rule_id", "rule_key", "payer_rule_id", "rule_type", "code"} and item:
                identifiers.add(str(item).lower())
            elif key in {"rule", "rule_ref", "rule_refs", "rules"}:
                identifiers.update(_rule_identifiers(item))
    elif isinstance(value, list):
        for item in value:
            identifiers.update(_rule_identifiers(item))
    elif value:
        identifiers.add(str(value).lower())
    return identifiers


def _rule_ref_matches(reference: dict[str, Any], rule: dict[str, Any]) -> bool:
    reference_ids = _rule_identifiers(reference)
    return bool(reference_ids and reference_ids.intersection(_rule_identifiers(rule)))


def _requires_source_value(path: str) -> bool:
    return not path.startswith("canonical_claim.amount")


def _group_from_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "field_path": entries[0]["field_path"],
        "issues": [],
        "history": [],
        "issue_ids": sorted({value for item in entries for value in item.get("issue_ids", [])}),
        "issue_codes": sorted({value for item in entries for value in item.get("issue_codes", [])}),
    }


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
