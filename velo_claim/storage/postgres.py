from __future__ import annotations

import json
import os
from velo_claim.core.enums import PriorAuthStatus,ExternalTransactionStatus
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
        self._pool = None
        try:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self.dsn,
                kwargs={"row_factory": self._dict_row},
                min_size=int(os.getenv("POSTGRES_POOL_MIN_SIZE", "1")),
                max_size=int(os.getenv("POSTGRES_POOL_MAX_SIZE", "10")),
                open=True,
            )
        except ImportError:
            self._pool = None

    def _connect(self):
        if self._pool is not None:
            return self._pool.connection()
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
    def update_prior_auth_submitted(self, request_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE prior_auth_request
                SET submitted_at = now(), status = %s, updated_at = now()
                WHERE id = %s
                """,
                (str(PriorAuthStatus.WAITING_FOR_PAYER), request_id),
            )

    def insert_submission_attempt(self, claim_id: str, data: dict[str, Any]) -> str:
        submission_id = data.get("submission_id") or str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO submission_attempt (id, claim_id, channel, object_uri, response_status, payer_response)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    submission_id,
                    claim_id,
                    str(data.get("channel") or "SIMULATED"),
                    data.get("object_uri"),
                    str(data.get("response_status")) if data.get("response_status") else None,
                    json.dumps(data.get("payer_response")) if data.get("payer_response") is not None else None,
                ),
            )
        return submission_id

    def update_submission_response(self, submission_id: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE submission_attempt
                SET response_status = %s, payer_response = %s
                WHERE id = %s
                """,
                (
                    str(data.get("response_status")) if data.get("response_status") else None,
                    json.dumps(data.get("payer_response")) if data.get("payer_response") is not None else None,
                    submission_id,
                ),
            )

    def cancel_latest_submission(self, claim_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE submission_attempt
                SET response_status = %s, updated_at = now()
                WHERE id = (
                    SELECT id FROM submission_attempt
                    WHERE claim_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                RETURNING *
                """,
                (str(ExternalTransactionStatus.CANCELLED), claim_id),
            ).fetchone()
            return dict(row) if row else None
    def insert_prior_auth_response(self, request_id: str, data: dict[str, Any]) -> str:
        # prior_auth_response.claim_id is NOT NULL, so pull it from the parent
        # request row rather than requiring the caller to supply it.
        with self._connect() as conn:
            parent = conn.execute(
                "SELECT claim_id FROM prior_auth_request WHERE id = %s",
                (request_id,),
            ).fetchone()
            claim_id = parent["claim_id"] if parent else None

            response_id = data.get("response_id") or str(uuid4())
            conn.execute(
                """
                INSERT INTO prior_auth_response
                    (id, request_id, claim_id, payer_response, pre_auth_ref, status, received_via, object_uri)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    response_id,
                    request_id,
                    claim_id,
                    json.dumps(data.get("payer_response")) if data.get("payer_response") is not None else None,
                    data.get("pre_auth_ref"),
                    str(data.get("status")) if data.get("status") else None,
                    str(data.get("received_via")) if data.get("received_via") else None,
                    data.get("object_uri"),
                ),
            )
        return response_id

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
            columns = _table_columns(conn, "claim_payload")
            status_column = "status" if "status" in columns else "payload_status"
            conn.execute(
                f"""
                INSERT INTO claim_payload (claim_id, version, standard, payload_type, object_uri, sha256_hash, {status_column})
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

    def insert_eligibility_check(self, claim_id: str, data: dict[str, Any]) -> str:
        check_id = data.get("id") or str(uuid4())
        result = data.get("result") or {}
        result_data = result.get("data") or {}
        eligibility_input = data.get("input") or {}
        payer_response = result_data.get("payer_response") or {}
        outcome = _response_value(payer_response, "outcome", "status", "result", "decision")

        values_by_column: dict[str, Any] = {
            "id": check_id,
            "request_id": data.get("request_id"),
            "claim_id": claim_id,
            "patient_id": eligibility_input.get("patient_id"),
            "payer_id": eligibility_input.get("payer_id"),
            "plan_id": data.get("plan_id"),
            "service_date": eligibility_input.get("service_date"),
            "status": str(result.get("status") or "FAIL_HOLD_CRITICAL"),
            "outcome": outcome,
            "coverage_ref": data.get("coverage_ref"),
            "member_id": data.get("member_id"),
            "eligibility_ref": result_data.get("eligibility_ref"),
            "voi_ref": data.get("voi_ref"),
            "benefit_summary": result_data.get("benefit_summary") or {},
            "payer_response": payer_response,
            "object_uri": data.get("object_uri"),
            "ttl_expires_at": data.get("ttl_expires_at"),
        }

        with self._connect() as conn:
            table_columns = _table_columns(conn, "eligibility_check")
            insert_columns = [name for name in values_by_column if name in table_columns]
            placeholders = [
                "%s::jsonb" if name in {"benefit_summary", "payer_response"} else "%s"
                for name in insert_columns
            ]
            values = [
                json.dumps(values_by_column[name], default=str)
                if name in {"benefit_summary", "payer_response"}
                else values_by_column[name]
                for name in insert_columns
            ]
            conn.execute(
                f"""
                INSERT INTO eligibility_check ({", ".join(insert_columns)})
                VALUES ({", ".join(placeholders)})
                """,
                values,
            )
        return check_id

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

    def insert_prior_auth_request(self, claim_id: str | None, data: dict[str, Any]) -> str:
        request_id = data.get("request_id") or str(uuid4())
        display_id = data.get("display_id") or f"PA-{uuid4().hex[:12].upper()}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO prior_auth_request (id, claim_id, standard, object_uri, status,display_id)
                VALUES (%s, %s, %s, %s, %s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (request_id, claim_id, str(data.get("standard")), data.get("object_uri"), str(data.get("status")),display_id),
            )
        return request_id

    def link_prior_auth_request_to_claim(self, request_id: str, claim_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE prior_auth_request
                SET claim_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (claim_id, request_id),
            )

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
            columns = _table_columns(conn, "validation_report")
            insert_columns = ["id", "claim_id", "version", "score", "final_status"]
            values = [
                report_id,
                claim_id,
                int(data.get("version", 1)),
                int(data.get("score", 0)),
                str(data.get("final_status")),
            ]
            if "report" in columns:
                insert_columns.append("report")
                values.append(json.dumps(data.get("report", {}), default=str))
            if "object_uri" in columns:
                insert_columns.append("object_uri")
                values.append(data.get("object_uri"))
            placeholders = ", ".join(["%s"] * len(insert_columns))
            conn.execute(
                f"""
                INSERT INTO validation_report ({", ".join(insert_columns)})
                VALUES ({placeholders})
                """,
                values,
            )
        return report_id

    def insert_validation_issue(self, report_id: str, issue: dict[str, Any]) -> None:
        with self._connect() as conn:
            columns = _table_columns(conn, "validation_issue")
            claim_id = issue.get("claim_id")
            if "claim_id" in columns and not claim_id:
                row = conn.execute(
                    "SELECT claim_id FROM validation_report WHERE id = %s",
                    (report_id,),
                ).fetchone()
                claim_id = row["claim_id"] if row else None

            insert_columns = ["report_id", "check_type", "severity", "code", "message", "field"]
            values = [
                report_id,
                issue.get("check_type"),
                str(issue.get("severity")),
                issue.get("code"),
                issue.get("message"),
                issue.get("field"),
            ]
            optional_values = {
                "claim_id": claim_id,
                "suggestion": issue.get("suggestion"),
                "agent": issue.get("agent"),
                "node": issue.get("node"),
            }
            for column, value in optional_values.items():
                if column in columns:
                    insert_columns.append(column)
                    values.append(value)
            placeholders = ", ".join(["%s"] * len(insert_columns))
            conn.execute(
                f"""
                INSERT INTO validation_issue ({", ".join(insert_columns)})
                VALUES ({placeholders})
                """,
                values,
            )

    def insert_audit_event(self, claim_id: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO claim (claim_id, status, jurisdiction, payer_id, provider_id, patient_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_id) DO NOTHING
                """,
                (claim_id, "DRAFT_BUILDING", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"),
            )
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
    def get_prior_auth_request(self, request_id_or_display_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, claim_id, standard, object_uri, submitted_at,
                       status, payer_transaction_id, created_at, updated_at, display_id
                FROM prior_auth_request
                WHERE id::text = %s OR display_id = %s
                """,
                (request_id_or_display_id, request_id_or_display_id),
            ).fetchone()
        return dict(row) if row else None


    def get_latest_prior_auth_response(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, request_id, claim_id, payer_response, pre_auth_ref,
                       status, received_via, object_uri, received_at, created_at
                FROM prior_auth_response
                WHERE request_id = %s
                ORDER BY received_at DESC
                LIMIT 1
                """,
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_claim_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM claim_queue_view
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]
            except Exception as exc:
                if "claim_queue_view" not in str(exc):
                    raise
                conn.rollback()

            claim_columns = _table_columns(conn, "claim")
            payload_columns = _table_columns(conn, "claim_payload")
            payload_status_expr = (
                "status::text"
                if "status" in payload_columns
                else "payload_status::text"
                if "payload_status" in payload_columns
                else "NULL::text"
            )
            claim_standard_expr = (
                "c.claim_standard"
                if "claim_standard" in claim_columns
                else "c.claim_format::text"
                if "claim_format" in claim_columns
                else "NULL::text"
            )
            rows = conn.execute(
                """
                WITH latest_payload AS (
                    SELECT DISTINCT ON (claim_id)
                        claim_id,
                        version AS payload_version,
                        standard AS claim_standard,
                        payload_type,
                        object_uri AS claim_payload_uri,
                        sha256_hash AS claim_payload_hash,
                        {payload_status_expr} AS payload_status
                    FROM claim_payload
                    ORDER BY claim_id, version DESC
                ),
                latest_report AS (
                    SELECT DISTINCT ON (claim_id)
                        claim_id,
                        score AS validation_score,
                        final_status::text AS validation_status
                    FROM validation_report
                    ORDER BY claim_id, created_at DESC
                )
                SELECT
                    c.claim_id,
                    c.status::text AS status,
                    c.jurisdiction,
                    COALESCE(lp.claim_standard::text, {claim_standard_expr}) AS claim_standard,
                    c.payer_id,
                    c.provider_id,
                    c.patient_id,
                    COALESCE(lr.validation_score, 0) AS validation_score,
                    lr.validation_status,
                    lp.payload_version,
                    lp.payload_type::text AS payload_type,
                    lp.claim_payload_uri,
                    lp.claim_payload_hash,
                    lp.payload_status,
                    c.updated_at
                FROM claim c
                LEFT JOIN latest_payload lp ON lp.claim_id = c.claim_id
                LEFT JOIN latest_report lr ON lr.claim_id = c.claim_id
                ORDER BY c.updated_at DESC
                LIMIT %s
                """.format(
                    payload_status_expr=payload_status_expr,
                    claim_standard_expr=claim_standard_expr,
                ),
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_claim_detail(self, claim_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            claim = conn.execute("SELECT * FROM claim WHERE claim_id = %s", (claim_id,)).fetchone()
            if not claim:
                return None
            route = conn.execute("SELECT * FROM route_decision WHERE claim_id = %s", (claim_id,)).fetchone()
            version = conn.execute(
                """
                SELECT *
                FROM claim_version
                WHERE claim_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (claim_id,),
            ).fetchone()
            payload = conn.execute(
                """
                SELECT *
                FROM claim_payload
                WHERE claim_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (claim_id,),
            ).fetchone()
            report = conn.execute(
                """
                SELECT *
                FROM validation_report
                WHERE claim_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (claim_id,),
            ).fetchone()
            issues = []
            if report:
                issues = conn.execute(
                    """
                    SELECT *
                    FROM validation_issue
                    WHERE report_id = %s
                    ORDER BY created_at ASC
                    """,
                    (report["id"],),
                ).fetchall()
            eligibility = conn.execute(
                """
                SELECT *
                FROM eligibility_check
                WHERE claim_id = %s
                ORDER BY checked_at DESC
                LIMIT 1
                """,
                (claim_id,),
            ).fetchone()
            pa_requests = conn.execute(
                """
                SELECT *
                FROM prior_auth_request
                WHERE claim_id = %s
                ORDER BY created_at ASC
                """,
                (claim_id,),
            ).fetchall()
            pa_responses = conn.execute(
                """
                SELECT r.*
                FROM prior_auth_response r
                JOIN prior_auth_request q ON q.id = r.request_id
                WHERE q.claim_id = %s
                ORDER BY r.received_at ASC
                """,
                (claim_id,),
            ).fetchall()
            audit_events = conn.execute(
                """
                SELECT *
                FROM audit_event
                WHERE claim_id = %s
                ORDER BY ts ASC
                LIMIT 500
                """,
                (claim_id,),
            ).fetchall()
        return {
            **dict(claim),
            "route": dict(route)["route"] if route else {},
            "route_row": dict(route) if route else {},
            "canonical_claim": dict(version).get("canonical_claim", {}) if version else {},
            "source_context": dict(version).get("source_context", {}) if version else {},
            "claim_version": dict(version) if version else {},
            "claim_payload": dict(payload) if payload else {},
            "validation_report": dict(report).get("report", {}) if report else {},
            "validation_report_row": dict(report) if report else {},
            "validation_issues": [dict(row) for row in issues],
            "eligibility_result": dict(eligibility) if eligibility else {},
            "prior_auth": {
                "requests": [dict(row) for row in pa_requests],
                "responses": [dict(row) for row in pa_responses],
                "latest_request": dict(pa_requests[-1]) if pa_requests else None,
                "latest_response": dict(pa_responses[-1]) if pa_responses else None,
            },
            "audit_events": [dict(row) for row in audit_events],
        }

    def update_claim_status(self, claim_id: str, status: str, metadata: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            columns = _table_columns(conn, "claim")
            if "metadata" in columns:
                conn.execute(
                    """
                    UPDATE claim
                    SET status = %s,
                        metadata = metadata || %s::jsonb,
                        updated_at = now()
                    WHERE claim_id = %s
                    """,
                    (status, json.dumps(metadata or {}, default=str), claim_id),
                )
                return
            conn.execute(
                """
                UPDATE claim
                SET status = %s,
                    updated_at = now()
                WHERE claim_id = %s
                """,
                (status, claim_id),
            )


def _payload_type(value: Any) -> str:
    text = str(value)
    return "xml" if text == "application/xml" else text


def _table_columns(conn: Any, table_name: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        """,
        (table_name,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def _response_value(response: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = response.get(key)
        if value is not None:
            return str(value)
    return None
