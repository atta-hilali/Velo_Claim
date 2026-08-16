from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from velo_claim.storage.interfaces import (
    CacheStoreInterface,
    DuplicateRecordError,
    ObjectStoreInterface,
    RepositoryInterface,
)
from velo_claim.core.utils import utc_now
from velo_claim.core.enums import ExternalTransactionStatus, PriorAuthStatus


@dataclass(slots=True)
class InMemoryRepository(RepositoryInterface):
    claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    claim_versions: list[dict[str, Any]] = field(default_factory=list)
    route_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    claim_payloads: list[dict[str, Any]] = field(default_factory=list)
    pa_payloads: list[dict[str, Any]] = field(default_factory=list)
    prior_auth_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    prior_auth_responses: list[dict[str, Any]] = field(default_factory=list)
    eligibility_checks: list[dict[str, Any]] = field(default_factory=list)
    validation_reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    validation_issues: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    callback_events: dict[str, dict[str, Any]] = field(default_factory=dict)
    submission_attempts: list[dict[str, Any]] = field(default_factory=list)
    payer_rule_sets: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def upsert_claim(self, claim_id: str, data: dict[str, Any]) -> None:
        existing = self.claims.get(claim_id, {})
        self.claims[claim_id] = {**existing, **data, "updated_at": utc_now()}
        self.claims[claim_id].setdefault("created_at", utc_now())

    def insert_claim_version(self, claim_id: str, version: int, data: dict[str, Any]) -> None:
        self.claim_versions.append({"claim_id": claim_id, "version": version, **data, "created_at": utc_now()})

    def put_route_decision(self, claim_id: str, route: dict[str, Any]) -> None:
        existing = self.route_decisions.get(claim_id)
        if existing and existing.get("route") != route:
            raise DuplicateRecordError(f"Route decision already exists for claim {claim_id}.")
        self.route_decisions[claim_id] = {"claim_id": claim_id, "route": route, "decided_at": utc_now()}

    def get_route_decision(self, claim_id: str) -> dict[str, Any] | None:
        return self.route_decisions.get(claim_id)

    def count_route_decisions(self, claim_id: str) -> int:
        return 1 if claim_id in self.route_decisions else 0

    def insert_claim_payload(self, claim_id: str, version: int, data: dict[str, Any]) -> None:
        self.claim_payloads.append({"claim_id": claim_id, "version": version, **data, "created_at": utc_now()})

    def latest_claim_payload(self, claim_id: str) -> dict[str, Any] | None:
        rows = [row for row in self.claim_payloads if row["claim_id"] == claim_id]
        return max(rows, key=lambda row: row["version"]) if rows else None

    def insert_eligibility_check(self, claim_id: str, data: dict[str, Any]) -> str:
        check_id = data.get("id") or str(uuid4())
        self.eligibility_checks.append(
            {
                "id": check_id,
                "claim_id": claim_id,
                **data,
                "checked_at": data.get("checked_at") or utc_now(),
                "created_at": utc_now(),
            }
        )
        return check_id

    def insert_pa_payload(self, claim_id: str, version: int, data: dict[str, Any]) -> None:
        self.pa_payloads.append({"claim_id": claim_id, "version": version, **data, "created_at": utc_now()})

    def insert_prior_auth_request(self, claim_id: str, data: dict[str, Any]) -> str:
        request_id = data.get("request_id") or str(uuid4())
        self.prior_auth_requests[request_id] = {
            "request_id": request_id,
            "claim_id": claim_id,
            **data,
            "created_at": utc_now(),
        }
        return request_id

    def find_prior_auth_response(self, claim_id: str, payer_id: str, cpt_code: str) -> dict[str, Any] | None:
        for response in reversed(self.prior_auth_responses):
            if (
                response.get("claim_id") == claim_id
                and response.get("payer_id") == payer_id
                and cpt_code in response.get("cpt_codes", [])
            ):
                return response
        return None

    def insert_prior_auth_response(self, request_id: str, data: dict[str, Any]) -> None:
        request = self.prior_auth_requests.get(request_id, {})
        self.prior_auth_responses.append(
            {
                "request_id": request_id,
                "claim_id": request.get("claim_id"),
                **data,
                "received_at": utc_now(),
            }
        )

    def update_prior_auth_submitted(self, request_id: str) -> None:
        request = self.prior_auth_requests.get(request_id)
        if request:
            request.update(
                submitted_at=utc_now(),
                status=str(PriorAuthStatus.WAITING_FOR_PAYER),
                updated_at=utc_now(),
            )

    def insert_submission_attempt(self, claim_id: str, data: dict[str, Any]) -> str:
        submission_id = data.get("submission_id") or str(uuid4())
        if not any(row.get("id") == submission_id for row in self.submission_attempts):
            self.submission_attempts.append(
                {
                    "id": submission_id,
                    "claim_id": claim_id,
                    **data,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
        return submission_id

    def update_submission_response(self, submission_id: str, data: dict[str, Any]) -> None:
        for attempt in self.submission_attempts:
            if attempt.get("id") == submission_id:
                attempt.update(data, updated_at=utc_now())
                return

    def cancel_latest_submission(self, claim_id: str) -> dict[str, Any] | None:
        attempts = [row for row in self.submission_attempts if row.get("claim_id") == claim_id]
        if not attempts:
            return None
        latest = max(attempts, key=lambda row: row.get("created_at", datetime.min.replace(tzinfo=UTC)))
        latest.update(response_status=str(ExternalTransactionStatus.CANCELLED), updated_at=utc_now())
        return latest

    def link_prior_auth_request_to_claim(self, request_id: str, claim_id: str) -> None:
        request = self.prior_auth_requests.get(request_id)
        if request:
            request.update(claim_id=claim_id, updated_at=utc_now())

    def get_prior_auth_request(self, request_id_or_display_id: str) -> dict[str, Any] | None:
        request = self.prior_auth_requests.get(request_id_or_display_id)
        if request:
            return request
        return next(
            (
                row
                for row in self.prior_auth_requests.values()
                if row.get("display_id") == request_id_or_display_id
            ),
            None,
        )

    def get_latest_prior_auth_response(self, request_id: str) -> dict[str, Any] | None:
        responses = [row for row in self.prior_auth_responses if row.get("request_id") == request_id]
        return max(responses, key=lambda row: row.get("received_at", datetime.min.replace(tzinfo=UTC))) if responses else None

    def insert_validation_report(self, claim_id: str, data: dict[str, Any]) -> str:
        report_id = data.get("report_id") or str(uuid4())
        self.validation_reports[report_id] = {
            "report_id": report_id,
            "claim_id": claim_id,
            **data,
            "created_at": utc_now(),
        }
        return report_id

    def insert_validation_issue(self, report_id: str, issue: dict[str, Any]) -> None:
        self.validation_issues.append({"report_id": report_id, **issue, "created_at": utc_now()})

    def insert_audit_event(self, claim_id: str, data: dict[str, Any]) -> None:
        self.audit_events.append({"claim_id": claim_id, **data, "ts": data.get("ts") or utc_now()})

    def insert_callback_event(self, claim_id: str, idempotency_key: str, data: dict[str, Any]) -> None:
        if idempotency_key in self.callback_events:
            raise DuplicateRecordError(f"Callback already processed: {idempotency_key}")
        self.callback_events[idempotency_key] = {
            "claim_id": claim_id,
            "idempotency_key": idempotency_key,
            **data,
            "received_at": utc_now(),
        }

    def find_duplicate_submission(self, claim_id: str, payer_id: str, fingerprint: str) -> dict[str, Any] | None:
        for attempt in self.submission_attempts:
            if (
                attempt.get("claim_id") != claim_id
                and attempt.get("payer_id") == payer_id
                and attempt.get("fingerprint") == fingerprint
            ):
                return attempt
        return None

    def upsert_payer_rule_set(self, payer_id: str, plan_id: str, data: dict[str, Any]) -> None:
        self.payer_rule_sets[(payer_id, plan_id)] = {
            "payer_id": payer_id,
            "plan_id": plan_id,
            **data,
            "loaded_at": utc_now(),
        }

    def get_cached_payer_rule_set(self, payer_id: str, plan_id: str) -> dict[str, Any] | None:
        return self.payer_rule_sets.get((payer_id, plan_id))

    def list_claim_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = [self._claim_detail(claim_id) for claim_id in self.claims]
        rows = [row for row in rows if row]
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def get_claim_detail(self, claim_id: str) -> dict[str, Any] | None:
        return self._claim_detail(claim_id)

    def update_claim_status(self, claim_id: str, status: str, metadata: dict[str, Any] | None = None) -> None:
        existing = self.claims.get(claim_id, {})
        self.claims[claim_id] = {
            **existing,
            "claim_id": claim_id,
            "status": status,
            "metadata": {**existing.get("metadata", {}), **(metadata or {})},
            "updated_at": utc_now(),
        }
        self.claims[claim_id].setdefault("created_at", utc_now())

    def _claim_detail(self, claim_id: str) -> dict[str, Any] | None:
        claim = self.claims.get(claim_id)
        if not claim:
            return None

        latest_payload = self.latest_claim_payload(claim_id)
        versions = [row for row in self.claim_versions if row["claim_id"] == claim_id]
        latest_version = max(versions, key=lambda row: row["version"]) if versions else {}
        reports = [row for row in self.validation_reports.values() if row.get("claim_id") == claim_id]
        latest_report = max(reports, key=lambda row: row.get("created_at", "")) if reports else {}
        report_id = latest_report.get("report_id")
        validation_issues = [
            issue for issue in self.validation_issues if not report_id or issue.get("report_id") == report_id
        ]
        latest_eligibility = next(
            (
                row
                for row in reversed(self.eligibility_checks)
                if row.get("claim_id") == claim_id
                or row.get("input", {}).get("claim_id") == claim_id
                or row.get("result", {}).get("claim_id") == claim_id
            ),
            None,
        )
        pa_requests = [
            {**request, "request_id": request_id}
            for request_id, request in self.prior_auth_requests.items()
            if request.get("claim_id") == claim_id
        ]
        pa_responses = [response for response in self.prior_auth_responses if response.get("claim_id") == claim_id]
        audit_events = [event for event in self.audit_events if event.get("claim_id") == claim_id]

        canonical_claim = latest_version.get("canonical_claim", {})
        route = self.route_decisions.get(claim_id, {}).get("route", latest_version.get("route", {}))
        source_context = latest_version.get("source_context", {})

        return {
            **claim,
            "claim_id": claim_id,
            "route": route,
            "routing_context": latest_version.get("routing_context", {}),
            "canonical_claim": canonical_claim,
            "source_context": source_context,
            "claim_payload": latest_payload,
            "validation_report": latest_report.get("report", {}),
            "validation_report_row": latest_report,
            "validation_issues": validation_issues,
            "eligibility_result": latest_eligibility or {},
            "prior_auth": {
                "requests": pa_requests,
                "responses": pa_responses,
                "latest_request": pa_requests[-1] if pa_requests else None,
                "latest_response": pa_responses[-1] if pa_responses else None,
            },
            "audit_events": audit_events,
        }


@dataclass(slots=True)
class InMemoryObjectStore(ObjectStoreInterface):
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    prefix: str = "memory://velo-claim"

    def put_text(self, key: str, value: str, content_type: str = "text/plain") -> str:
        uri = f"{self.prefix}/{key.strip('/')}"
        self.objects[uri] = {"value": value, "content_type": content_type, "created_at": utc_now()}
        return uri

    def get_text(self, uri: str) -> str:
        if uri not in self.objects:
            raise KeyError(f"Object not found: {uri}")
        return self.objects[uri]["value"]


@dataclass(slots=True)
class InMemoryCacheStore(CacheStoreInterface):
    values: dict[str, tuple[Any, datetime | None]] = field(default_factory=dict)

    def set(self, key: str, value: Any, ttl_seconds: int | None = None, nx: bool = False) -> bool:
        self._expire()
        if nx and key in self.values:
            return False
        expires_at = None
        if ttl_seconds is not None:
            expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        self.values[key] = (value, expires_at)
        return True

    def get(self, key: str) -> Any | None:
        self._expire()
        row = self.values.get(key)
        return row[0] if row else None

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def keys(self, prefix: str = "") -> list[str]:
        self._expire()
        return [key for key in self.values if key.startswith(prefix)]

    def _expire(self) -> None:
        now = datetime.now(UTC)
        expired = [key for key, (_, expires_at) in self.values.items() if expires_at and expires_at <= now]
        for key in expired:
            self.values.pop(key, None)
