from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from velo_claim.storage.interfaces import DuplicateRecordError, RepositoryInterface


class PostgresRepository(RepositoryInterface):
    """Production PostgreSQL repository.

    Uses `psycopg` when installed. It is intentionally not used by the default
    local container so the package remains runnable without infrastructure.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn:
            raise ValueError("DATABASE_URL or dsn is required for PostgresRepository.")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use PostgresRepository: pip install psycopg[binary]") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row

    def _connect(self):
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def upsert_claim(self, claim_id: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO claim (claim_id, status, jurisdiction, payer_id, provider_id, patient_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_id) DO UPDATE SET
                  status = EXCLUDED.status,
                  jurisdiction = EXCLUDED.jurisdiction,
                  payer_id = EXCLUDED.payer_id,
                  provider_id = EXCLUDED.provider_id,
                  patient_id = EXCLUDED.patient_id,
                  updated_at = now()
                """,
                (
                    claim_id,
                    str(data.get("status", "DRAFT")),
                    data.get("jurisdiction"),
                    data.get("payer_id"),
                    data.get("provider_id"),
                    data.get("patient_id"),
                ),
            )

    def insert_claim_version(self, claim_id: str, version: int, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO claim_version (claim_id, version, canonical_claim, route, source_context)
                VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                ON CONFLICT (claim_id, version) DO NOTHING
                """,
                (
                    claim_id,
                    version,
                    json.dumps(data.get("canonical_claim", {}), default=str),
                    json.dumps(data.get("route", {}), default=str),
                    json.dumps(data.get("source_context", {}), default=str),
                ),
            )

    def put_route_decision(self, claim_id: str, route: dict[str, Any]) -> None:
        with self._connect() as conn:
            existing = conn.execute("SELECT route FROM route_decision WHERE claim_id = %s", (claim_id,)).fetchone()
            if existing and existing["route"] != route:
                raise DuplicateRecordError(f"Route decision already exists for claim {claim_id}.")
            conn.execute(
                """
                INSERT INTO route_decision (claim_id, route)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (claim_id) DO NOTHING
                """,
                (claim_id, json.dumps(route, default=str)),
            )

    def get_route_decision(self, claim_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT claim_id, route, decided_at FROM route_decision WHERE claim_id = %s", (claim_id,)).fetchone()
            return dict(row) if row else None

    def count_route_decisions(self, claim_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT count(*) AS count FROM route_decision WHERE claim_id = %s", (claim_id,)).fetchone()
            return int(row["count"]) if row else 0

    def insert_claim_payload(self, claim_id: str, version: int, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO claim_payload (claim_id, version, standard, payload_type, object_uri, sha256_hash, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_id, version) DO NOTHING
                """,
                (
                    claim_id,
                    version,
                    str(data.get("standard")),
                    _payload_type(data.get("payload_type")),
                    data.get("object_uri"),
                    data.get("sha256_hash"),
                    str(data.get("status")),
                ),
            )

    def latest_claim_payload(self, claim_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM claim_payload WHERE claim_id = %s ORDER BY version DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
            return dict(row) if row else None

    def insert_pa_payload(self, claim_id: str, version: int, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pa_payload (claim_id, version, standard, payload_type, object_uri, sha256_hash, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_id, version) DO NOTHING
                """,
                (
                    claim_id,
                    version,
                    str(data.get("standard")),
                    _payload_type(data.get("payload_type")),
                    data.get("object_uri"),
                    data.get("sha256_hash"),
                    str(data.get("status")),
                ),
            )

    def insert_prior_auth_request(self, claim_id: str, data: dict[str, Any]) -> str:
        request_id = data.get("request_id") or str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO prior_auth_request (id, claim_id, standard, object_uri, submitted_at, status)
                VALUES (%s, %s, %s, %s, now(), %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (request_id, claim_id, str(data.get("standard")), data.get("object_uri"), str(data.get("status"))),
            )
        return request_id

    def find_prior_auth_response(self, claim_id: str, payer_id: str, cpt_code: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*
                FROM prior_auth_response r
                JOIN prior_auth_request q ON q.id = r.request_id
                WHERE q.claim_id = %s
                  AND r.payer_response->>'payer_id' = %s
                  AND r.payer_response->'cpt_codes' ? %s
                ORDER BY r.received_at DESC
                LIMIT 1
                """,
                (claim_id, payer_id, cpt_code),
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            data.update(data.get("payer_response") or {})
            return data

    def insert_prior_auth_response(self, request_id: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO prior_auth_response (request_id, payer_response, pre_auth_ref, status)
                VALUES (%s, %s::jsonb, %s, %s)
                """,
                (request_id, json.dumps(data, default=str), data.get("pre_auth_ref"), str(data.get("status"))),
            )

    def insert_validation_report(self, claim_id: str, data: dict[str, Any]) -> str:
        report_id = data.get("report_id") or str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO validation_report (id, claim_id, version, score, final_status, report)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    report_id,
                    claim_id,
                    int(data.get("version", 1)),
                    int(data.get("score", 0)),
                    str(data.get("final_status")),
                    json.dumps(data.get("report", {}), default=str),
                ),
            )
        return report_id

    def insert_validation_issue(self, report_id: str, issue: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO validation_issue (report_id, check_type, severity, code, message, field)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    report_id,
                    issue.get("check_type"),
                    str(issue.get("severity")),
                    issue.get("code"),
                    issue.get("message"),
                    issue.get("field"),
                ),
            )

    def insert_audit_event(self, claim_id: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_event (claim_id, agent, node, event_type, payload, ts)
                VALUES (%s, %s, %s, %s, %s::jsonb, COALESCE(%s::timestamptz, now()))
                """,
                (
                    claim_id,
                    data.get("agent"),
                    data.get("node"),
                    data.get("event_type"),
                    json.dumps(data.get("payload", {}), default=str),
                    data.get("ts"),
                ),
            )

    def insert_callback_event(self, claim_id: str, idempotency_key: str, data: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO callback_event (claim_id, job_id, source, raw_payload, idempotency_key)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        claim_id,
                        data.get("job_id"),
                        str(data.get("source")),
                        json.dumps(data.get("raw_payload", {}), default=str),
                        idempotency_key,
                    ),
                )
        except Exception as exc:
            if "duplicate key" in str(exc).lower() or "unique" in str(exc).lower():
                raise DuplicateRecordError(f"Callback already processed: {idempotency_key}") from exc
            raise

    def find_duplicate_submission(self, claim_id: str, payer_id: str, fingerprint: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM submission_attempt
                WHERE claim_id <> %s
                  AND payer_response->>'payer_id' = %s
                  AND payer_response->>'fingerprint' = %s
                LIMIT 1
                """,
                (claim_id, payer_id, fingerprint),
            ).fetchone()
            return dict(row) if row else None

    def upsert_payer_rule_set(self, payer_id: str, plan_id: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO payer_rule_version (
                    payer_id, plan_id, rule_set, effective_from, eligibility_ttl_seconds, loaded_at
                )
                VALUES (%s, %s, %s::jsonb, %s, %s, now())
                """,
                (
                    payer_id,
                    plan_id,
                    json.dumps(data.get("rule_set", data), default=str),
                    data.get("effective_from"),
                    int(data.get("eligibility_ttl_seconds", 3600)),
                ),
            )

    def get_cached_payer_rule_set(self, payer_id: str, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payer_id, plan_id, rule_set, eligibility_ttl_seconds, loaded_at
                FROM payer_rule_version
                WHERE payer_id = %s AND plan_id = %s
                ORDER BY loaded_at DESC
                LIMIT 1
                """,
                (payer_id, plan_id),
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            rule_set = data.get("rule_set") or {}
            if isinstance(rule_set, dict):
                return {
                    **rule_set,
                    "payer_id": data["payer_id"],
                    "plan_id": data["plan_id"],
                    "eligibility_ttl_seconds": data["eligibility_ttl_seconds"],
                    "source": "CACHED",
                }
            return data


def _payload_type(value: Any) -> str:
    text = str(value)
    return "xml" if text == "application/xml" else text
