from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from velo_claim.payers.interface import PayerTransportInterface, TransportRequest, TransportResult


class HttpPayerTransport(PayerTransportInterface):
    """No-retry HTTP transport for FHIR or XML payer gateways.

    POST network failures are reported as delivery_unknown because the payer may
    have accepted the transaction before the connection failed.
    """

    def __init__(
        self,
        *,
        endpoints: dict[str, str],
        poll_endpoints: dict[str, str] | None = None,
        access_token: str | None = None,
        timeout_seconds: int = 30,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.endpoints = {key: value for key, value in endpoints.items() if value}
        self.poll_endpoints = {key: value for key, value in (poll_endpoints or {}).items() if value}
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}

    def submit(self, request: TransportRequest) -> TransportResult:
        endpoint = self.endpoints.get(request.transaction_type)
        if not endpoint:
            return TransportResult("manual_required", request.correlation_id)
        headers = {
            "Content-Type": _wire_content_type(request.content_type),
            "Accept": "application/fhir+json, application/json, application/xml, text/xml",
            "X-Correlation-ID": request.correlation_id,
            **self.extra_headers,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        raw_request = urllib.request.Request(
            endpoint,
            data=request.payload.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(raw_request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                return _result_from_response(request.correlation_id, response.status, body, response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return TransportResult(
                status="rejected" if 400 <= exc.code < 500 else "error",
                correlation_id=request.correlation_id,
                response=_decode_body(body),
                diagnostic={"http_status": exc.code},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return TransportResult(
                status="delivery_unknown",
                correlation_id=request.correlation_id,
                diagnostic={"error_type": type(exc).__name__, "message": str(exc)},
            )

    def poll(self, request: TransportRequest, external_id: str | None = None) -> TransportResult:
        endpoint = self.poll_endpoints.get(request.transaction_type)
        if not endpoint or not external_id:
            return TransportResult("manual_required", request.correlation_id, external_id=external_id)
        query = urllib.parse.urlencode({"id": external_id})
        headers = {"Accept": "application/fhir+json, application/json, application/xml, text/xml", **self.extra_headers}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        raw_request = urllib.request.Request(f"{endpoint}?{query}", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(raw_request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                return _result_from_response(request.correlation_id, response.status, body, response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return TransportResult(
                status="rejected" if 400 <= exc.code < 500 else "error",
                correlation_id=request.correlation_id,
                external_id=external_id,
                response=_decode_body(body),
                diagnostic={"http_status": exc.code},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return TransportResult(
                "error",
                request.correlation_id,
                external_id=external_id,
                diagnostic={"error_type": type(exc).__name__, "message": str(exc)},
            )


def _result_from_response(correlation_id: str, status_code: int, body: str, headers: Any) -> TransportResult:
    decoded = _decode_body(body)
    outcome = _outcome(decoded)
    status = {
        "queued": "queued",
        "pending": "queued",
        "pended": "pended",
        "partial": "partial",
        "error": "error",
        "failed": "error",
        "denied": "rejected",
        "rejected": "rejected",
        "complete": "complete",
        "completed": "complete",
        "approved": "complete",
        "eligible": "complete",
    }.get(outcome, "complete" if body and status_code < 300 else "accepted")
    external_id = _external_id(decoded) or headers.get("X-Request-ID") or headers.get("Location")
    retry_after = headers.get("Retry-After")
    return TransportResult(
        status=status,
        correlation_id=correlation_id,
        external_id=str(external_id) if external_id else None,
        response=decoded,
        retry_after_seconds=int(retry_after) if str(retry_after or "").isdigit() else None,
        diagnostic={"http_status": status_code},
    )


def _decode_body(body: str) -> Any:
    text = body.strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return {"payload": body}


def _outcome(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("outcome", "status", "decision", "result"):
            if value.get(key):
                return str(value[key]).strip().lower()
        if value.get("resourceType") == "Bundle":
            for entry in value.get("entry", []):
                resource = entry.get("resource", {}) if isinstance(entry, dict) else {}
                if resource.get("outcome"):
                    return str(resource["outcome"]).strip().lower()
    return ""


def _external_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("transaction_id", "request_id", "id", "identifier"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _wire_content_type(content_type: str) -> str:
    return "application/fhir+json" if content_type in {"fhir_bundle_json", "application/fhir+json"} else content_type
