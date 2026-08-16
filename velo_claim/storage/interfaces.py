from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DuplicateRecordError(RuntimeError):
    pass


class RepositoryInterface(ABC):
    @abstractmethod
    def upsert_claim(self, claim_id: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def insert_claim_version(self, claim_id: str, version: int, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def put_route_decision(self, claim_id: str, route: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_route_decision(self, claim_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def count_route_decisions(self, claim_id: str) -> int: ...

    @abstractmethod
    def insert_claim_payload(self, claim_id: str, version: int, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def latest_claim_payload(self, claim_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def insert_eligibility_check(self, claim_id: str, data: dict[str, Any]) -> str: ...

    @abstractmethod
    def insert_pa_payload(self, claim_id: str, version: int, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def insert_prior_auth_request(self, claim_id: str, data: dict[str, Any]) -> str: ...

    @abstractmethod
    def find_prior_auth_response(self, claim_id: str, payer_id: str, cpt_code: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def insert_prior_auth_response(self, request_id: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def update_prior_auth_submitted(self, request_id: str) -> None: ...

    @abstractmethod
    def insert_submission_attempt(self, claim_id: str, data: dict[str, Any]) -> str: ...

    @abstractmethod
    def update_submission_response(self, submission_id: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def cancel_latest_submission(self, claim_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def link_prior_auth_request_to_claim(self, request_id: str, claim_id: str) -> None: ...

    @abstractmethod
    def get_prior_auth_request(self, request_id_or_display_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_latest_prior_auth_response(self, request_id: str) -> dict[str, Any] | None: ...


    @abstractmethod
    def insert_validation_report(self, claim_id: str, data: dict[str, Any]) -> str: ...

    @abstractmethod
    def insert_validation_issue(self, report_id: str, issue: dict[str, Any]) -> None: ...

    @abstractmethod
    def insert_audit_event(self, claim_id: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def insert_callback_event(self, claim_id: str, idempotency_key: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def find_duplicate_submission(self, claim_id: str, payer_id: str, fingerprint: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def upsert_payer_rule_set(self, payer_id: str, plan_id: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_cached_payer_rule_set(self, payer_id: str, plan_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_claim_summaries(self, limit: int = 100) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_claim_detail(self, claim_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_claim_status(self, claim_id: str, status: str, metadata: dict[str, Any] | None = None) -> None: ...


class ObjectStoreInterface(ABC):
    @abstractmethod
    def put_text(self, key: str, value: str, content_type: str = "text/plain") -> str: ...

    @abstractmethod
    def get_text(self, uri: str) -> str: ...


class CacheStoreInterface(ABC):
    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int | None = None, nx: bool = False) -> bool: ...

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...
