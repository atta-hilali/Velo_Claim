from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface


class MockNeo4jClient(Neo4jClientInterface):
    """Static mock with realistic defaults until the production KG is loaded."""

    _VALID_PAIRS: set[tuple[str, str]] = {
        ("M54.5", "99213"),
        ("J06.9", "99213"),
        ("J18.9", "99213"),
        ("Z00.00", "99386"),
        ("I10", "93000"),
        ("I10", "99213"),
        ("E11.9", "83036"),
        ("K21.0", "43239"),
        ("M17.11", "27447"),
    }

    _PA_REQUIRED: set[tuple[str, str, str]] = {
        ("A001", "TH4QF", "43239"),
        ("A001", "TH4QF", "70553"),
        ("B002", "PLN01", "27447"),
        ("B002", "PLN01", "29827"),
    }

    _BUNDLED: dict[str, list[str]] = {
        "99213": ["99212", "99211"],
        "43239": ["43235"],
    }

    _REQUIRED_DOCS: dict[str, list[str]] = {
        "70553": ["RADIOLOGY_REFERRAL", "CLINICAL_NOTES"],
        "27447": ["OPERATIVE_REPORT", "PRE_AUTH_APPROVAL"],
        "43239": ["ENDOSCOPY_REPORT"],
    }

    def query_icd_cpt_compatibility(self, icd_code: str, cpt_code: str) -> bool | None:
        icd = normalize_code(icd_code)
        cpt = normalize_code(cpt_code)
        if not icd or not cpt:
            return None
        return (icd, cpt) in self._VALID_PAIRS

    def query_pa_required(self, payer_id: str, plan_id: str, cpt_code: str) -> bool:
        return (
            normalize_code(payer_id),
            normalize_code(plan_id),
            normalize_code(cpt_code),
        ) in self._PA_REQUIRED

    def query_bundled_procedures(self, cpt_code: str) -> list[str]:
        return list(self._BUNDLED.get(normalize_code(cpt_code), []))

    def query_required_documents(self, cpt_code: str) -> list[str]:
        return list(self._REQUIRED_DOCS.get(normalize_code(cpt_code), []))
