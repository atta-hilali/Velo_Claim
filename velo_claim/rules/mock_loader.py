from velo_claim.core.models import PayerRuleSet
from velo_claim.core.utils import normalize_code
from velo_claim.rules.interface import PayerRuleLoaderInterface


class MockPayerRuleLoader(PayerRuleLoaderInterface):
    """Static rule loader behind the same interface as future portal loaders."""

    _RULES: dict[tuple[str, str], PayerRuleSet] = {
        ("A001", "TH4QF"): PayerRuleSet(
            payer_id="A001",
            plan_id="TH4QF",
            eligibility_ttl_seconds=86400,
            pa_required_cpt_codes=["43239", "70553"],
            bundling_rules={"99213": ["99212", "99211"]},
            required_doc_types={"70553": ["RADIOLOGY_REFERRAL", "CLINICAL_NOTES"]},
            submission_channel="SHAFAFIYA_GATEWAY_OR_PORTAL",
            source="MOCK",
        ),
        ("B002", "PLN01"): PayerRuleSet(
            payer_id="B002",
            plan_id="PLN01",
            eligibility_ttl_seconds=43200,
            pa_required_cpt_codes=["27447", "29827"],
            bundling_rules={},
            required_doc_types={"27447": ["OPERATIVE_REPORT", "PRE_AUTH_APPROVAL"]},
            submission_channel="NPHIES_GATEWAY",
            source="MOCK",
        ),
        ("A002", "DXB_BASIC"): PayerRuleSet(
            payer_id="A002",
            plan_id="DXB_BASIC",
            eligibility_ttl_seconds=86400,
            pa_required_cpt_codes=["70553"],
            bundling_rules={"99213": ["99212", "99211"]},
            required_doc_types={"70553": ["RADIOLOGY_REFERRAL"]},
            submission_channel="ECLAIMLINK_GATEWAY_OR_PORTAL",
            source="MOCK",
        ),
    }

    _DEFAULT = PayerRuleSet(
        payer_id="UNKNOWN",
        plan_id="UNKNOWN",
        eligibility_ttl_seconds=3600,
        pa_required_cpt_codes=[],
        bundling_rules={},
        required_doc_types={},
        submission_channel="MANUAL_PORTAL",
        source="MOCK",
    )

    def load(self, payer_id: str, plan_id: str) -> PayerRuleSet:
        key = (normalize_code(payer_id), normalize_code(plan_id))
        return self._RULES.get(key) or PayerRuleSet(
            payer_id=payer_id or self._DEFAULT.payer_id,
            plan_id=plan_id or self._DEFAULT.plan_id,
            eligibility_ttl_seconds=self._DEFAULT.eligibility_ttl_seconds,
            pa_required_cpt_codes=[],
            bundling_rules={},
            required_doc_types={},
            submission_channel=self._DEFAULT.submission_channel,
            source="MOCK",
        )
