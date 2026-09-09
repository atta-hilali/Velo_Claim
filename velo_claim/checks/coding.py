from __future__ import annotations

import json
import os
import urllib.request

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.models import KnowledgeStatus, normalize_code_system


def check_coding_consistency(state: dict, kg_client: Neo4jClientInterface) -> CheckResult:
    claim = state.get("canonical_claim", {})
    diagnoses = claim.get("diagnoses", [])
    procedures = claim.get("procedures", []) or claim.get("line_items", [])
    issues: list[CheckIssue] = []
    evidence: list[dict] = []
    if not diagnoses:
        issues.append(_issue("DIAGNOSIS_MISSING", Severity.ERROR, "canonical_claim.diagnoses",
                             "No diagnosis code is present.", 20))
    if not procedures:
        issues.append(_issue("PROCEDURE_MISSING", Severity.ERROR, "canonical_claim.procedures",
                             "No procedure code is present.", 20))

    for procedure in procedures:
        if not diagnoses or not procedure.get("code"):
            continue
        procedure_system = normalize_code_system(procedure.get("system"))
        results = [
            kg_client.query_diagnosis_procedure_compatibility(
                diagnosis_code=diagnosis.get("code", ""),
                procedure_code=procedure.get("code", ""),
                diagnosis_system=normalize_code_system(diagnosis.get("system"), diagnosis=True),
                procedure_system=procedure_system,
                service_date=procedure.get("service_date") or claim.get("encounter", {}).get("service_date"),
            )
            for diagnosis in diagnoses
            if diagnosis.get("code")
        ]
        evidence.extend(result.to_dict() for result in results)
        if any(result.status == KnowledgeStatus.SUPPORTED for result in results):
            continue
        if any(result.status == KnowledgeStatus.UNAVAILABLE for result in results):
            issues.append(
                _issue(
                    "KG_CODING_UNAVAILABLE", Severity.ERROR,
                    f"canonical_claim.procedures.{procedure.get('code')}",
                    "Coding knowledge could not be checked because Neo4j is unavailable.", 20,
                    evidence={"kg_results": [result.to_dict() for result in results]},
                )
            )
            continue
        explicitly_unsupported = any(result.status == KnowledgeStatus.NOT_SUPPORTED for result in results)
        llm_evidence = _llm_coding_review(state, procedure) if _llm_enabled() else None
        issues.append(
            _issue(
                "DIAGNOSIS_PROCEDURE_NOT_SUPPORTED" if explicitly_unsupported else "DIAGNOSIS_PROCEDURE_KNOWLEDGE_UNKNOWN",
                Severity.ERROR if explicitly_unsupported else Severity.WARNING,
                f"canonical_claim.procedures.{procedure.get('code')}",
                (
                    f"The KG explicitly does not support {procedure_system} {procedure.get('code')} for the supplied diagnoses."
                    if explicitly_unsupported
                    else f"No explicit KG support edge was found for {procedure_system} {procedure.get('code')}; compatibility is unknown."
                ),
                20 if explicitly_unsupported else 5,
                evidence={"kg_results": [result.to_dict() for result in results], "llm_review": llm_evidence},
            )
        )
    requires_review = any(
        result.get("source") == "NEO4J"
        and result.get("status") in {
            str(KnowledgeStatus.UNKNOWN),
            str(KnowledgeStatus.UNAVAILABLE),
            str(KnowledgeStatus.CONFLICT),
        }
        for result in evidence
    )
    status = "PASS" if not issues else "REVIEW_REQUIRED" if requires_review else "REVIEW"
    return CheckResult("CODING", status, issues, {"kg_results": evidence})


def _issue(
    code: str, severity: Severity, field: str, message: str, penalty: int,
    *, evidence: dict | None = None,
) -> CheckIssue:
    return CheckIssue(
        code=code, severity=severity, check_type="CODING", field=field, message=message,
        suggestion="Route to coding review and verify authoritative coding evidence.",
        penalty=penalty, evidence=evidence or {},
    )


def _llm_enabled() -> bool:
    return os.getenv("USE_CODING_LLM", os.getenv("USE_VALIDATION_LLM", "false")).lower() in {"1", "true", "yes"}


def _llm_coding_review(state: dict, procedure: dict) -> dict | None:
    base_url = os.getenv("CODING_LLM_BASE_URL") or os.getenv("VALIDATION_LLM_BASE_URL")
    if not base_url:
        return {"status": "not_configured"}
    payload = {
        "model": os.getenv("CODING_LLM_MODEL") or os.getenv("VALIDATION_LLM_MODEL", "medgemma"),
        "messages": [
            {"role": "system", "content": "Return strict JSON with fields: supported, reason, missing_evidence."},
            {"role": "user", "content": json.dumps({
                "diagnoses": state.get("canonical_claim", {}).get("diagnoses", []),
                "procedure": procedure,
                "attachments": state.get("canonical_claim", {}).get("attachments", []),
            }, default=str)},
        ],
        "temperature": 0,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {"status": "raw", "content": content}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
