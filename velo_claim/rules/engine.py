from velo_claim.core.models import PayerRuleSet
from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface


def pa_required_for_code(
    *,
    payer_id: str,
    plan_id: str,
    cpt_code: str,
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
) -> bool:
    code = normalize_code(cpt_code)
    return code in {normalize_code(item) for item in payer_rules.pa_required_cpt_codes} or kg_client.query_pa_required(
        payer_id,
        plan_id,
        code,
    )


def required_documents_for_code(
    *,
    cpt_code: str,
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
) -> list[str]:
    code = normalize_code(cpt_code)
    docs = set(payer_rules.required_doc_types.get(code, []))
    docs.update(kg_client.query_required_documents(code))
    return sorted(docs)


def bundled_codes_for_code(
    *,
    cpt_code: str,
    payer_rules: PayerRuleSet,
    kg_client: Neo4jClientInterface,
) -> list[str]:
    code = normalize_code(cpt_code)
    bundled = set(payer_rules.bundling_rules.get(code, []))
    bundled.update(kg_client.query_bundled_procedures(code))
    return sorted(bundled)
