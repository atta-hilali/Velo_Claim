from __future__ import annotations

from velo_claim.core.models import PayerRuleSet
from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.models import KnowledgeResult, KnowledgeStatus, normalize_code_system


def prior_auth_requirement_for_code(
    *, payer_id: str, plan_id: str, procedure_code: str, procedure_system: str,
    service_date: str | None, payer_rules: PayerRuleSet, kg_client: Neo4jClientInterface,
) -> KnowledgeResult:
    code = normalize_code(procedure_code)
    system = normalize_code_system(procedure_system)
    file_required = code in {normalize_code(item) for item in payer_rules.pa_required_cpt_codes}
    kg_result = kg_client.query_prior_authorization(
        payer_id=payer_id,
        plan_id=plan_id,
        procedure_code=code,
        procedure_system=system,
        service_date=service_date,
    )
    evidence = {"kg": kg_result.to_dict(), "payer_rule_source": payer_rules.source, "file_required": file_required}
    if file_required and kg_result.status == KnowledgeStatus.NOT_REQUIRED:
        return KnowledgeResult(
            status=KnowledgeStatus.CONFLICT, query="prior_authorization", source="RULE_ENGINE",
            payer_id=payer_id, plan_id=plan_id, procedure_code=code, procedure_system=system,
            reason="File payer rules require PA while Neo4j explicitly says PA is not required.",
            evidence=evidence,
        )
    if file_required:
        return KnowledgeResult(
            status=KnowledgeStatus.REQUIRED, query="prior_authorization", source=payer_rules.source,
            payer_id=payer_id, plan_id=plan_id, procedure_code=code, procedure_system=system,
            evidence=evidence,
        )
    return _with_evidence(kg_result, evidence)


def required_documents_for_code(
    *, procedure_code: str | None = None, procedure_system: str = "CPT",
    cpt_code: str | None = None, payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
) -> tuple[list[str], KnowledgeResult]:
    code = normalize_code(procedure_code or cpt_code)
    documents = set(payer_rules.required_doc_types.get(code, []))
    result = kg_client.query_documentation_requirements(
        procedure_code=code,
        procedure_system=normalize_code_system(procedure_system),
    )
    documents.update(result.evidence.get("required_documents", []))
    return sorted({normalize_code(item) for item in documents if item}), result


def bundled_codes_for_code(
    *, procedure_code: str | None = None, procedure_system: str = "CPT",
    cpt_code: str | None = None, payer_id: str | None = None,
    plan_id: str | None = None, service_date: str | None = None,
    payer_rules: PayerRuleSet, kg_client: Neo4jClientInterface,
) -> tuple[list[str], KnowledgeResult]:
    code = normalize_code(procedure_code or cpt_code)
    bundled = set(payer_rules.bundling_rules.get(code, []))
    result = kg_client.query_bundling(
        procedure_code=code,
        procedure_system=normalize_code_system(procedure_system),
        payer_id=payer_id,
        plan_id=plan_id,
        service_date=service_date,
    )
    bundled.update(result.evidence.get("bundled_codes", []))
    return sorted({normalize_code(item) for item in bundled if item}), result


def pa_required_for_code(
    *, payer_id: str, plan_id: str, cpt_code: str, payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
) -> bool:
    """Compatibility wrapper for callers not yet carrying a code system."""
    return prior_auth_requirement_for_code(
        payer_id=payer_id,
        plan_id=plan_id,
        procedure_code=cpt_code,
        procedure_system="CPT",
        service_date=None,
        payer_rules=payer_rules,
        kg_client=kg_client,
    ).status == KnowledgeStatus.REQUIRED


def _with_evidence(result: KnowledgeResult, evidence: dict) -> KnowledgeResult:
    return KnowledgeResult(
        status=result.status, query=result.query, source=result.source,
        entity_id=result.entity_id, payer_id=result.payer_id, plan_id=result.plan_id,
        diagnosis_code=result.diagnosis_code, diagnosis_system=result.diagnosis_system,
        procedure_code=result.procedure_code, procedure_system=result.procedure_system,
        effective_from=result.effective_from, effective_to=result.effective_to,
        reason=result.reason, evidence=evidence,
    )
