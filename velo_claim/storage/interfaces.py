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
    def insert_validation_issue(self, report_id: str, issue: dict[str, Any]) -> str: ...

    @abstractmethod
    def get_validation_report(self, report_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_latest_validation_report(self, claim_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_validation_issues(self, report_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_current_claim_version(self, claim_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_claim_version(self, claim_id: str, version: int) -> dict[str, Any] | None: ...

    @abstractmethod
    def create_correction_cycle(self, claim_id: str, data: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def get_correction_cycle(self, cycle_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_correction_cycles(self, claim_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def insert_correction_suggestion(self, data: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def get_correction_suggestion(self, suggestion_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_correction_suggestions(self, cycle_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def insert_correction_review(self, data: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def list_correction_reviews(self, suggestion_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def update_correction_suggestion_status(self, suggestion_id: str, status: str) -> None: ...

    @abstractmethod
    def update_correction_cycle_status(self, cycle_id: str, status: str) -> None: ...

    @abstractmethod
    def get_approved_correction_rule(
        self, issue_code: str, check_type: str, field_path: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_correction_history(self, claim_id: str, field_path: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def commit_corrected_claim(
        self,
        *,
        claim_id: str,
        cycle_id: str,
        expected_base_version: int,
        new_version: int,
        version_data: dict[str, Any],
        payload_data: dict[str, Any],
    ) -> None: ...

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
    def put_bytes(self, key: str, value: bytes, content_type: str = "application/octet-stream") -> str: ...

    @abstractmethod
    def put_text(self, key: str, value: str, content_type: str = "text/plain") -> str: ...

    @abstractmethod
    def get_bytes(self, uri: str) -> bytes: ...

    @abstractmethod
    def get_text(self, uri: str) -> str: ...


class CacheStoreInterface(ABC):
    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int | None = None, nx: bool = False) -> bool: ...

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...
