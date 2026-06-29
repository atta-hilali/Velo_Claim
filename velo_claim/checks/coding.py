from __future__ import annotations

import json
import os
import urllib.request

from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult
from velo_claim.kg.interface import Neo4jClientInterface


def check_coding_consistency(state: dict, kg_client: Neo4jClientInterface) -> CheckResult:
    claim = state.get("canonical_claim", {})
    diagnoses = claim.get("diagnoses", [])
    procedures = claim.get("procedures", [])
    issues: list[CheckIssue] = []
    if not diagnoses:
        issues.append(
            CheckIssue(
                code="DIAGNOSIS_MISSING",
                severity=Severity.ERROR,
                check_type="CODING",
                field="canonical_claim.diagnoses",
                message="No diagnosis code is present.",
                suggestion="Add diagnosis code from Condition or encounter documentation.",
                penalty=20,
            )
        )
    if not procedures:
        issues.append(
            CheckIssue(
                code="PROCEDURE_MISSING",
                severity=Severity.ERROR,
                check_type="CODING",
                field="canonical_claim.procedures",
                message="No procedure code is present.",
                suggestion="Add CPT/CDT code from Procedure or charge item.",
                penalty=20,
            )
        )
    for proc in procedures:
        if not diagnoses:
            continue
        compatible = any(kg_client.query_icd_cpt_compatibility(diag.get("code"), proc.get("code")) for diag in diagnoses)
        if not compatible:
            llm_evidence = _llm_coding_review(state, proc) if _llm_enabled() else None
            issues.append(
                CheckIssue(
                    code="ICD_CPT_COMPATIBILITY_REVIEW",
                    severity=Severity.WARNING,
                    check_type="CODING",
                    field=f"canonical_claim.procedures.{proc.get('code')}",
                    message=f"No KG compatibility edge found for CPT {proc.get('code')} and current diagnoses.",
                    suggestion="Route to coding review or use LLM-assisted coding enrichment.",
                    penalty=5,
                    evidence={"llm_required": True, "llm_review": llm_evidence},
                )
            )
    return CheckResult("CODING", "PASS" if not issues else "REVIEW", issues)


def _llm_enabled() -> bool:
    return os.getenv("USE_CODING_LLM", os.getenv("USE_VALIDATION_LLM", "false")).lower() in {"1", "true", "yes"}


def _llm_coding_review(state: dict, procedure: dict) -> dict | None:
    base_url = os.getenv("CODING_LLM_BASE_URL") or os.getenv("VALIDATION_LLM_BASE_URL")
    if not base_url:
        return {"status": "not_configured"}
    payload = {
        "model": os.getenv("CODING_LLM_MODEL") or os.getenv("VALIDATION_LLM_MODEL", "medgemma"),
        "messages": [
            {
                "role": "system",
                "content": "Return strict JSON with fields: supported, reason, missing_evidence.",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "diagnoses": state.get("canonical_claim", {}).get("diagnoses", []),
                        "procedure": procedure,
                        "attachments": state.get("canonical_claim", {}).get("attachments", []),
                    },
                    default=str,
                ),
            },
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
