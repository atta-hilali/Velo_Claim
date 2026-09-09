from __future__ import annotations

from velo_claim.core.utils import normalize_code
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.models import KnowledgeResult, KnowledgeStatus, normalize_code_system


class MockNeo4jClient(Neo4jClientInterface):
    """Explicit test double. Production configuration must never select it implicitly."""

    _VALID_PAIRS: set[tuple[str, str]] = {
        ("M54.5", "99213"), ("J06.9", "99213"), ("J18.9", "99213"),
        ("Z00.00", "99386"), ("I10", "93000"), ("I10", "99213"),
        ("E11.9", "83036"), ("K21.0", "43239"), ("M17.11", "27447"),
    }
    _PA_REQUIRED: set[tuple[str, str, str]] = {
        ("A001", "TH4QF", "43239"), ("A001", "TH4QF", "70553"),
        ("B002", "PLN01", "27447"), ("B002", "PLN01", "29827"),
    }
    _BUNDLED = {"99213": ["99212", "99211"], "43239": ["43235"]}
    _REQUIRED_DOCS = {
        "70553": ["RADIOLOGY_REFERRAL", "CLINICAL_NOTES"],
        "27447": ["OPERATIVE_REPORT", "PRE_AUTH_APPROVAL"],
        "43239": ["ENDOSCOPY_REPORT"],
    }

    def query_diagnosis_procedure_compatibility(
        self, *, diagnosis_code: str, procedure_code: str, diagnosis_system: str,
        procedure_system: str, service_date: str | None = None,
    ) -> KnowledgeResult:
        pair = (normalize_code(diagnosis_code), normalize_code(procedure_code))
        return KnowledgeResult(
            status=KnowledgeStatus.SUPPORTED if pair in self._VALID_PAIRS else KnowledgeStatus.UNKNOWN,
            query="diagnosis_procedure_compatibility", source="MOCK",
            diagnosis_code=pair[0], diagnosis_system=normalize_code_system(diagnosis_system, diagnosis=True),
            procedure_code=pair[1], procedure_system=normalize_code_system(procedure_system),
        )

    def query_prior_authorization(
        self, *, payer_id: str, plan_id: str, procedure_code: str,
        procedure_system: str, service_date: str | None = None,
    ) -> KnowledgeResult:
        key = (normalize_code(payer_id), normalize_code(plan_id), normalize_code(procedure_code))
        return KnowledgeResult(
            status=KnowledgeStatus.REQUIRED if key in self._PA_REQUIRED else KnowledgeStatus.UNKNOWN,
            query="prior_authorization", source="MOCK", payer_id=key[0], plan_id=key[1],
            procedure_code=key[2], procedure_system=normalize_code_system(procedure_system),
        )

    def query_plan_benefit(
        self, *, payer_id: str, plan_id: str, procedure_code: str,
        procedure_system: str, service_date: str | None = None,
        employer_policy_id: str | None = None,
    ) -> KnowledgeResult:
        return KnowledgeResult(
            status=KnowledgeStatus.UNKNOWN, query="plan_benefit", source="MOCK",
            payer_id=normalize_code(payer_id), plan_id=normalize_code(plan_id),
            procedure_code=normalize_code(procedure_code),
            procedure_system=normalize_code_system(procedure_system),
        )

    def query_bundling(
        self, *, procedure_code: str, procedure_system: str,
        payer_id: str | None = None, plan_id: str | None = None,
        service_date: str | None = None,
    ) -> KnowledgeResult:
        code = normalize_code(procedure_code)
        bundled = list(self._BUNDLED.get(code, []))
        return KnowledgeResult(
            status=KnowledgeStatus.SUPPORTED if bundled else KnowledgeStatus.UNKNOWN,
            query="bundling", source="MOCK", procedure_code=code,
            procedure_system=normalize_code_system(procedure_system),
            evidence={"bundled_codes": bundled},
        )

    def query_documentation_requirements(
        self, *, procedure_code: str, procedure_system: str,
    ) -> KnowledgeResult:
        code = normalize_code(procedure_code)
        documents = list(self._REQUIRED_DOCS.get(code, []))
        return KnowledgeResult(
            status=KnowledgeStatus.REQUIRED if documents else KnowledgeStatus.UNKNOWN,
            query="documentation_requirements", source="MOCK", procedure_code=code,
            procedure_system=normalize_code_system(procedure_system),
            evidence={"required_documents": documents},
        )

    def diagnostics(self) -> dict:
        return {"backend": "mock", "connectivity": "healthy", "database": None,
                "mock_fallback": True, "production_safe": False}
