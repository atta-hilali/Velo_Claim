from __future__ import annotations

import json
import os
from datetime import date, datetime
from fnmatch import fnmatch
from typing import Any
from uuid import uuid4

from velo_claim.core.enums import ExternalTransactionStatus, PriorAuthStatus
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
                  jurisdiction = COALESCE(EXCLUDED.jurisdiction, claim.jurisdiction),
                  payer_id = COALESCE(EXCLUDED.payer_id, claim.payer_id),
                  provider_id = COALESCE(EXCLUDED.provider_id, claim.provider_id),
                  patient_id = COALESCE(EXCLUDED.patient_id, claim.patient_id),
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
        with self._connect() as conn:
            parent = conn.execute(
                "SELECT claim_id FROM prior_auth_request WHERE id = %s",
                (request_id,),
            ).fetchone()
            claim_id = parent["claim_id"] if parent else None
            if not parent:
                raise ValueError(f"Cannot store PA response: request not found: {request_id}")

            response_id = data.get("response_id") or str(uuid4())
            payer_response = data.get("payer_response")
            if payer_response is None:
                payer_response = data
            columns = _table_columns(conn, "prior_auth_response")
            values_by_column = {
                "id": response_id,
                "request_id": request_id,
                "claim_id": claim_id,
                "payer_response": json.dumps(payer_response, default=str),
                "normalized_response": json.dumps(data, default=str),
                "status": str(data.get("status") or "unknown"),
                "outcome": data.get("outcome"),
                "decision": data.get("decision"),
                "pre_auth_ref": data.get("pre_auth_ref"),
                "payer_id": data.get("payer_id"),
                "cpt_codes": json.dumps(data.get("cpt_codes", []), default=str),
                "valid_from": _database_date(data.get("valid_from")),
                "valid_to": _database_date(data.get("valid_to")),
                "message": data.get("message"),
                "source": str(data.get("source") or data.get("received_via") or "WEBHOOK"),
                "received_via": str(data.get("received_via") or data.get("source") or "WEBHOOK"),
                "raw_payload_uri": data.get("raw_payload_uri") or data.get("object_uri"),
                "object_uri": data.get("object_uri") or data.get("raw_payload_uri"),
            }
            insert_columns = [name for name in values_by_column if name in columns]
            placeholders = ", ".join(["%s"] * len(insert_columns))
            conn.execute(
                f"INSERT INTO prior_auth_response ({', '.join(insert_columns)}) "
                f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                [values_by_column[name] for name in insert_columns],
            )
        return response_id

    def insert_claim_version(self, claim_id: str, version: int, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM claim_version WHERE claim_id = %s AND version = %s",
                (claim_id, version),
            ).fetchone()
            if existing:
                return
            columns = _table_columns(conn, "claim_version")
            correction_column = ", correction_cycle_id" if "correction_cycle_id" in columns else ""
            correction_placeholder = ", %s" if "correction_cycle_id" in columns else ""
            values = [
                claim_id,
                version,
                data.get("parent_version"),
                json.dumps(data.get("canonical_claim", {}), default=str),
                json.dumps(data.get("route", {}), default=str),
                json.dumps(data.get("source_context", {}), default=str),
                json.dumps(data.get("routing_context", {}), default=str),
                data.get("rebuild_reason"),
                int(data.get("rebuild_attempt") or 0),
                data.get("created_by_agent"),
            ]
            if "correction_cycle_id" in columns:
                values.append(data.get("correction_cycle_id"))
            conn.execute(
                f"""
                INSERT INTO claim_version (
                    claim_id, version, parent_version, canonical_claim, route,
                    source_context, routing_context, rebuild_reason, rebuild_attempt,
                    is_current, created_by_agent{correction_column}
                )
                VALUES (
                    %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s, %s, TRUE, %s{correction_placeholder}
                )
                ON CONFLICT (claim_id, version) DO NOTHING
                """,
                values,
            )
            conn.execute(
                "UPDATE claim SET current_version = GREATEST(current_version, %s), updated_at = now() WHERE claim_id = %s",
                (version, claim_id),
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
            conn.execute(
                "UPDATE claim SET current_payload_version = GREATEST(current_payload_version, %s), updated_at = now() WHERE claim_id = %s",
                (version, claim_id),
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
                INSERT INTO pa_payload (claim_id, version, standard, payload_type, object_uri, sha256_hash, required_codes, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (claim_id, version) DO NOTHING
                """,
                (
                    claim_id,
                    version,
                    str(data.get("standard")),
                    _payload_type(data.get("payload_type")),
                    data.get("object_uri"),
                    data.get("sha256_hash"),
                    json.dumps(data.get("required_codes", []), default=str),
                    str(data.get("status")),
                ),
            )

    def insert_prior_auth_request(self, claim_id: str | None, data: dict[str, Any]) -> str:
        request_id = data.get("request_id") or str(uuid4())
        display_id = data.get("display_id") or f"PA-{uuid4().hex[:12].upper()}"
        with self._connect() as conn:
            columns = _table_columns(conn, "prior_auth_request")
            values_by_column = {
                "id": request_id,
                "claim_id": claim_id,
                "standard": str(data.get("standard")),
                "payload_type": _payload_type(data.get("payload_type")),
                "object_uri": data.get("object_uri"),
                "sha256_hash": data.get("sha256_hash") or data.get("payload_hash"),
                "required_codes": json.dumps(data.get("required_codes", []), default=str),
                "payer_id": data.get("payer_id"),
                "plan_id": data.get("plan_id"),
                "service_date": _database_date(data.get("service_date")),
                "status": str(data.get("status")),
                "request_payload": json.dumps(data.get("request_payload", {}), default=str),
                "callback_state": json.dumps(data.get("callback_state", {}), default=str),
                "display_id": display_id,
            }
            insert_columns = [name for name in values_by_column if name in columns]
            placeholders = ", ".join(["%s"] * len(insert_columns))
            conn.execute(
                f"INSERT INTO prior_auth_request ({', '.join(insert_columns)}) "
                f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                [values_by_column[name] for name in insert_columns],
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

    def insert_validation_issue(self, report_id: str, issue: dict[str, Any]) -> str:
        issue_id = issue.get("issue_id") or issue.get("id") or str(uuid4())
        with self._connect() as conn:
            columns = _table_columns(conn, "validation_issue")
            claim_id = issue.get("claim_id")
            if "claim_id" in columns and not claim_id:
                row = conn.execute(
                    "SELECT claim_id FROM validation_report WHERE id = %s",
                    (report_id,),
                ).fetchone()
                claim_id = row["claim_id"] if row else None

            insert_columns = ["id", "report_id", "check_type", "severity", "code", "message", "field"]
            values = [
                issue_id,
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
                "penalty": int(issue.get("penalty") or 0),
                "evidence": json.dumps(issue.get("evidence") or {}, default=str),
            }
            for column, value in optional_values.items():
                if column in columns:
                    insert_columns.append(column)
                    values.append(value)
            placeholders = ", ".join(
                "%s::jsonb" if column == "evidence" else "%s"
                for column in insert_columns
            )
            conn.execute(
                f"""
                INSERT INTO validation_issue ({", ".join(insert_columns)})
                VALUES ({placeholders})
                """,
                values,
            )
        return str(issue_id)

    def get_validation_report(self, report_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT *, id::text AS report_id FROM validation_report WHERE id = %s",
                (report_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_validation_report(self, claim_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *, id::text AS report_id
                FROM validation_report
                WHERE claim_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (claim_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_validation_issues(self, report_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *, id::text AS issue_id
                FROM validation_issue
                WHERE report_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (report_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_current_claim_version(self, claim_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM claim_version
                WHERE claim_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (claim_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_claim_version(self, claim_id: str, version: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM claim_version WHERE claim_id = %s AND version = %s",
                (claim_id, version),
            ).fetchone()
        return dict(row) if row else None

    def create_correction_cycle(self, claim_id: str, data: dict[str, Any]) -> dict[str, Any]:
        cycle_id = data.get("cycle_id") or data.get("id") or str(uuid4())
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO correction_cycle (
                    id, claim_id, validation_report_id, base_claim_version,
                    base_payload_version, cycle_number, status,
                    payer_rule_source_version, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (claim_id, validation_report_id, cycle_number)
                DO UPDATE SET updated_at = correction_cycle.updated_at
                RETURNING *, id::text AS cycle_id
                """,
                (
                    cycle_id,
                    claim_id,
                    data.get("validation_report_id"),
                    int(data.get("base_claim_version") or 0),
                    int(data.get("base_payload_version") or 0),
                    int(data.get("cycle_number") or 1),
                    str(data.get("status") or "GENERATING"),
                    data.get("payer_rule_source_version"),
                    json.dumps(data.get("metadata") or {}, default=str),
                ),
            ).fetchone()
        return dict(row)

    def get_correction_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT *, id::text AS cycle_id FROM correction_cycle WHERE id = %s",
                (cycle_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_correction_cycles(self, claim_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *, id::text AS cycle_id
                FROM correction_cycle
                WHERE claim_id = %s
                ORDER BY cycle_number ASC, created_at ASC
                """,
                (claim_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_correction_suggestion(self, data: dict[str, Any]) -> dict[str, Any]:
        suggestion_id = data.get("suggestion_id") or data.get("id") or str(uuid4())
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO correction_suggestion (
                    id, cycle_id, claim_id, validation_report_id,
                    base_claim_version, base_payload_version, issue_ids,
                    issue_codes, field_path, old_value, proposed_value,
                    source, confidence, rationale, evidence, rule_refs,
                    status, cycle_count, suggestion_hash
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s::jsonb, %s, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s, %s, %s
                )
                ON CONFLICT (suggestion_hash)
                DO UPDATE SET updated_at = correction_suggestion.updated_at
                RETURNING *, id::text AS suggestion_id
                """,
                (
                    suggestion_id,
                    data.get("cycle_id"),
                    data.get("claim_id"),
                    data.get("validation_report_id"),
                    int(data.get("base_claim_version") or 0),
                    int(data.get("base_payload_version") or 0),
                    json.dumps(data.get("issue_ids") or [], default=str),
                    json.dumps(data.get("issue_codes") or [], default=str),
                    data.get("field_path"),
                    json.dumps(data.get("old_value"), default=str),
                    json.dumps(data.get("proposed_value"), default=str),
                    str(data.get("source")),
                    float(data.get("confidence") or 0),
                    data.get("rationale") or "",
                    json.dumps(data.get("evidence") or {}, default=str),
                    json.dumps(data.get("rule_refs") or [], default=str),
                    str(data.get("status") or "PENDING_REVIEW"),
                    int(data.get("cycle_count") or 1),
                    data.get("suggestion_hash"),
                ),
            ).fetchone()
        return dict(row)

    def get_correction_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT *, id::text AS suggestion_id FROM correction_suggestion WHERE id = %s",
                (suggestion_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_correction_suggestions(self, cycle_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *, id::text AS suggestion_id
                FROM correction_suggestion
                WHERE cycle_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (cycle_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_correction_review(self, data: dict[str, Any]) -> dict[str, Any]:
        review_id = data.get("review_id") or data.get("id") or str(uuid4())
        suggestion_id = str(data.get("suggestion_id") or "")
        try:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT *, id::text AS review_id FROM correction_review WHERE suggestion_id = %s FOR UPDATE",
                    (suggestion_id,),
                ).fetchone()
                if existing:
                    if (
                        str(existing["decision"]) == str(data.get("decision"))
                        and existing["reviewer_id"] == data.get("reviewer_id")
                    ):
                        return dict(existing)
                    raise DuplicateRecordError(f"Correction suggestion already reviewed: {suggestion_id}")

                row = conn.execute(
                    """
                    INSERT INTO correction_review (
                        id, suggestion_id, decision, reviewer_id,
                        modified_value, comment, reviewed_at
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, now()))
                    RETURNING *, id::text AS review_id
                    """,
                    (
                        review_id,
                        suggestion_id,
                        str(data.get("decision")),
                        data.get("reviewer_id"),
                        json.dumps(data.get("modified_value"), default=str),
                        data.get("comment"),
                        data.get("reviewed_at"),
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE correction_suggestion SET status = %s, updated_at = now() WHERE id = %s",
                    (str(data.get("decision")), suggestion_id),
                )
                cycle_row = conn.execute(
                    "SELECT cycle_id FROM correction_suggestion WHERE id = %s",
                    (suggestion_id,),
                ).fetchone()
                if cycle_row:
                    self._refresh_correction_cycle_status(conn, str(cycle_row["cycle_id"]))
                return dict(row)
        except DuplicateRecordError:
            raise
        except Exception as exc:
            if "duplicate key" in str(exc).lower() or "unique" in str(exc).lower():
                raise DuplicateRecordError(f"Correction suggestion already reviewed: {suggestion_id}") from exc
            raise

    def list_correction_reviews(self, suggestion_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *, id::text AS review_id
                FROM correction_review
                WHERE suggestion_id = %s
                ORDER BY reviewed_at ASC
                """,
                (suggestion_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_correction_suggestion_status(self, suggestion_id: str, status: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE correction_suggestion
                SET status = %s, updated_at = now()
                WHERE id = %s
                RETURNING cycle_id
                """,
                (str(status), suggestion_id),
            ).fetchone()
            if row:
                self._refresh_correction_cycle_status(conn, str(row["cycle_id"]))

    def update_correction_cycle_status(self, cycle_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE correction_cycle SET status = %s, updated_at = now() WHERE id = %s",
                (str(status), cycle_id),
            )

    def get_approved_correction_rule(
        self, issue_code: str, check_type: str, field_path: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM correction_rule
                WHERE issue_code = %s
                  AND check_type = %s
                  AND status IN ('APPROVED', 'ACTIVE')
                  AND approved_by IS NOT NULL
                ORDER BY updated_at DESC
                """,
                (issue_code, check_type),
            ).fetchall()
        for row in rows:
            candidate = dict(row)
            if fnmatch(field_path, str(candidate.get("field_pattern") or "")):
                return candidate
        return None

    def list_correction_history(self, claim_id: str, field_path: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, s.id::text AS suggestion_id,
                       COALESCE(jsonb_agg(to_jsonb(r) ORDER BY r.reviewed_at)
                         FILTER (WHERE r.id IS NOT NULL), '[]'::jsonb) AS reviews
                FROM correction_suggestion s
                LEFT JOIN correction_review r ON r.suggestion_id = s.id
                WHERE s.claim_id = %s AND s.field_path = %s
                GROUP BY s.id
                ORDER BY s.created_at ASC
                """,
                (claim_id, field_path),
            ).fetchall()
        return [dict(row) for row in rows]

    def commit_corrected_claim(
        self,
        *,
        claim_id: str,
        cycle_id: str,
        expected_base_version: int,
        new_version: int,
        version_data: dict[str, Any],
        payload_data: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            claim = conn.execute(
                "SELECT current_version FROM claim WHERE claim_id = %s FOR UPDATE",
                (claim_id,),
            ).fetchone()
            if not claim:
                raise ValueError(f"Claim not found: {claim_id}")
            current = conn.execute(
                """
                SELECT version FROM claim_version
                WHERE claim_id = %s
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                (claim_id,),
            ).fetchone()
            if not current or int(current["version"]) != expected_base_version:
                raise DuplicateRecordError("The claim version changed before the correction could be applied.")
            cycle = conn.execute(
                "SELECT status FROM correction_cycle WHERE id = %s AND claim_id = %s FOR UPDATE",
                (cycle_id, claim_id),
            ).fetchone()
            if not cycle or str(cycle["status"]) != "READY_TO_APPLY":
                raise ValueError("Correction cycle is not ready to apply.")
            if new_version != expected_base_version + 1:
                raise ValueError("Corrected claim version must increment the base version by one.")

            version_row = conn.execute(
                """
                INSERT INTO claim_version (
                    claim_id, version, parent_version, canonical_claim, route,
                    source_context, routing_context, rebuild_reason, rebuild_attempt,
                    is_current, created_by_agent, correction_cycle_id
                )
                VALUES (
                    %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s, %s,
                    TRUE, %s, %s
                )
                RETURNING id
                """,
                (
                    claim_id,
                    new_version,
                    expected_base_version,
                    json.dumps(version_data.get("canonical_claim") or {}, default=str),
                    json.dumps(version_data.get("route") or {}, default=str),
                    json.dumps(version_data.get("source_context") or {}, default=str),
                    json.dumps(version_data.get("routing_context") or {}, default=str),
                    version_data.get("rebuild_reason") or "HUMAN_APPROVED_CORRECTION",
                    int(version_data.get("rebuild_attempt") or 0),
                    version_data.get("created_by_agent") or "CorrectionSuggesterAgent",
                    cycle_id,
                ),
            ).fetchone()
            payload_columns = _table_columns(conn, "claim_payload")
            payload_status_column = "status" if "status" in payload_columns else "payload_status"
            conn.execute(
                f"""
                INSERT INTO claim_payload (
                    claim_id, claim_version_id, version, standard, payload_type,
                    object_uri, sha256_hash, {payload_status_column}, generated_by_agent
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    claim_id,
                    version_row["id"],
                    new_version,
                    str(payload_data.get("standard")),
                    _payload_type(payload_data.get("payload_type")),
                    payload_data.get("object_uri"),
                    payload_data.get("sha256_hash"),
                    str(payload_data.get("status") or "DRAFT_BUILT"),
                    payload_data.get("generated_by_agent") or "CorrectionSuggesterAgent",
                ),
            )
            conn.execute(
                """
                UPDATE claim
                SET current_version = %s,
                    current_payload_version = %s,
                    status = %s,
                    updated_at = now()
                WHERE claim_id = %s
                """,
                (
                    new_version,
                    new_version,
                    str(payload_data.get("status") or "DRAFT_BUILT"),
                    claim_id,
                ),
            )
            conn.execute(
                """
                UPDATE correction_suggestion
                SET status = 'APPLIED', updated_at = now()
                WHERE cycle_id = %s AND status IN ('APPROVED', 'MODIFIED')
                """,
                (cycle_id,),
            )
            conn.execute(
                "UPDATE correction_cycle SET status = 'APPLIED', updated_at = now() WHERE id = %s",
                (cycle_id,),
            )

    @staticmethod
    def _refresh_correction_cycle_status(conn: Any, cycle_id: str) -> None:
        rows = conn.execute(
            "SELECT status FROM correction_suggestion WHERE cycle_id = %s",
            (cycle_id,),
        ).fetchall()
        statuses = [str(row["status"]) for row in rows]
        if not statuses:
            status = "GENERATING"
        elif "REJECTED" in statuses:
            status = "REJECTED"
        elif all(item in {"APPROVED", "MODIFIED"} for item in statuses):
            status = "READY_TO_APPLY"
        elif any(item in {"APPROVED", "MODIFIED", "REJECTED"} for item in statuses):
            status = "PARTIALLY_REVIEWED"
        else:
            status = "AWAITING_HUMAN_REVIEW"
        conn.execute(
            "UPDATE correction_cycle SET status = %s, updated_at = now() WHERE id = %s",
            (status, cycle_id),
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
            columns = _table_columns(conn, "prior_auth_request")
            display_predicate = " OR display_id = %s" if "display_id" in columns else ""
            params = (request_id_or_display_id, request_id_or_display_id) if display_predicate else (request_id_or_display_id,)
            row = conn.execute(
                f"SELECT * FROM prior_auth_request WHERE id::text = %s{display_predicate}",
                params,
            ).fetchone()
        return dict(row) if row else None


    def get_latest_prior_auth_response(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM prior_auth_response
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


def _payload_type(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return "xml" if text == "application/xml" else text


def _database_date(value: Any) -> str | date | None:
    if value in (None, "") or isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported database date: {value}")


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
