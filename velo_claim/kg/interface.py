from abc import ABC, abstractmethod

from velo_claim.kg.models import KnowledgeResult, KnowledgeStatus


class Neo4jClientInterface(ABC):
    """Shared interface for mock and production Neo4j clients."""

    @abstractmethod
    def query_diagnosis_procedure_compatibility(
        self,
        *,
        diagnosis_code: str,
        procedure_code: str,
        diagnosis_system: str,
        procedure_system: str,
        service_date: str | None = None,
    ) -> KnowledgeResult: ...

    @abstractmethod
    def query_prior_authorization(
        self,
        *,
        payer_id: str,
        plan_id: str,
        procedure_code: str,
        procedure_system: str,
        service_date: str | None = None,
    ) -> KnowledgeResult: ...

    @abstractmethod
    def query_plan_benefit(
        self,
        *,
        payer_id: str,
        plan_id: str,
        procedure_code: str,
        procedure_system: str,
        service_date: str | None = None,
        employer_policy_id: str | None = None,
    ) -> KnowledgeResult: ...

    @abstractmethod
    def query_bundling(
        self,
        *,
        procedure_code: str,
        procedure_system: str,
        payer_id: str | None = None,
        plan_id: str | None = None,
        service_date: str | None = None,
    ) -> KnowledgeResult: ...

    @abstractmethod
    def query_documentation_requirements(
        self,
        *,
        procedure_code: str,
        procedure_system: str,
    ) -> KnowledgeResult: ...

    @abstractmethod
    def diagnostics(self) -> dict: ...

    def query_icd_cpt_compatibility(self, icd_code: str, cpt_code: str) -> bool | None:
        result = self.query_diagnosis_procedure_compatibility(
            diagnosis_code=icd_code,
            procedure_code=cpt_code,
            diagnosis_system="ICD-10",
            procedure_system="CPT",
        )
        if result.status == KnowledgeStatus.SUPPORTED:
            return True
        if result.status == KnowledgeStatus.NOT_SUPPORTED:
            return False
        return None

    def query_pa_required(self, payer_id: str, plan_id: str, cpt_code: str) -> bool | None:
        result = self.query_prior_authorization(
            payer_id=payer_id,
            plan_id=plan_id,
            procedure_code=cpt_code,
            procedure_system="CPT",
        )
        if result.status == KnowledgeStatus.REQUIRED:
            return True
        if result.status == KnowledgeStatus.NOT_REQUIRED:
            return False
        return None

    def query_bundled_procedures(self, cpt_code: str) -> list[str]:
        result = self.query_bundling(procedure_code=cpt_code, procedure_system="CPT")
        return list(result.evidence.get("bundled_codes", []))

    def query_required_documents(self, cpt_code: str) -> list[str]:
        result = self.query_documentation_requirements(
            procedure_code=cpt_code,
            procedure_system="CPT",
        )
        return list(result.evidence.get("required_documents", []))
