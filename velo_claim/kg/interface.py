from abc import ABC, abstractmethod


class Neo4jClientInterface(ABC):
    """Shared interface for mock and production Neo4j clients."""

    @abstractmethod
    def query_icd_cpt_compatibility(self, icd_code: str, cpt_code: str) -> bool | None: ...

    @abstractmethod
    def query_pa_required(self, payer_id: str, plan_id: str, cpt_code: str) -> bool: ...

    @abstractmethod
    def query_bundled_procedures(self, cpt_code: str) -> list[str]: ...

    @abstractmethod
    def query_required_documents(self, cpt_code: str) -> list[str]: ...
