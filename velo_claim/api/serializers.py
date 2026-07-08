from __future__ import annotations

from datetime import date, datetime
from typing import Any

from velo_claim.storage.interfaces import ObjectStoreInterface


STATUS_TO_FRONTEND = {
    "READY_TO_SUBMIT": "ready",
    "DRAFT_BUILT": "review",
    "DRAFT_BUILDING": "review",
    "DRAFT_INVALID": "hold",
    "VALIDATED": "ready",
    "SUBMITTED": "submitted",
    "WAITING_FOR_PAYER": "waiting",
    "ACCEPTED": "submitted",
    "REJECTED": "hold",
    "NEEDS_REVIEW": "review",
    "NEEDS_PRIOR_AUTH": "review",
    "NEEDS_PAYLOAD_REBUILD": "review",
    "HOLD_CRITICAL": "hold",
}


def claim_for_api(detail: dict[str, Any], object_store: ObjectStoreInterface | None = None) -> dict[str, Any]:
    canonical = detail.get("canonical_claim") or {}
    patient = canonical.get("patient") or {}
    payer = canonical.get("payer") or {}
    provider = canonical.get("provider") or {}
    encounter = canonical.get("encounter") or {}
    amount = canonical.get("amount") or {}
    route = _plain(detail.get("route") or {})
    report = _plain(detail.get("validation_report") or {})
    report_row = _plain(detail.get("validation_report_row") or {})
    payload_row = _plain(detail.get("claim_payload") or {})
    payload_text = _payload_text(payload_row, object_store)
    eligibility = _eligibility_for_frontend(detail)
    prior_auth = _prior_auth_for_frontend(detail)
    issues = _issues_for_frontend(detail, report)

    claim_id = detail.get("claim_id") or canonical.get("claim_id")
    status = _frontend_status(
        report.get("status")
        or report.get("final_status")
        or report_row.get("final_status")
        or detail.get("payload_status")
        or detail.get("status")
    )
    score = int(report.get("score") or report_row.get("score") or detail.get("score") or 0)

    return _plain(
        {
            "id": claim_id,
            "claim_id": claim_id,
            "patient": patient.get("name") or detail.get("patient_name") or patient.get("id") or "Unknown Patient",
            "patient_name": patient.get("name") or detail.get("patient_name"),
            "mrn": patient.get("id") or detail.get("patient_id") or "-",
            "payer": payer.get("name") or detail.get("payer_name") or payer.get("id") or "Unknown Payer",
            "payer_name": payer.get("name") or detail.get("payer_name"),
            "payer_id": payer.get("id") or detail.get("payer_id"),
            "plan": payer.get("plan_id") or detail.get("plan_id") or "-",
            "jurisdiction": _route_value(route, "jurisdiction") or detail.get("jurisdiction") or "-",
            "format": _route_value(route, "claim_standard") or detail.get("claim_standard") or "-",
            "serviceDate": encounter.get("service_date") or detail.get("service_date") or "-",
            "service_date": encounter.get("service_date") or detail.get("service_date"),
            "score": score,
            "status": status,
            "updated": _relative_or_date(detail.get("updated_at") or detail.get("created_at")),
            "amount": _format_amount(amount, detail),
            "canonical_claim": canonical,
            "source_context": detail.get("source_context") or {},
            "route": route,
            "payload": _payload_for_frontend(canonical, payload_row, payload_text),
            "claim_payload": payload_text,
            "claim_payload_row": payload_row,
            "validation_report": report,
            "validation_report_row": report_row,
            "issues": issues,
            "eligibility": eligibility,
            "eligibility_result": eligibility,
            "priorAuth": prior_auth,
            "prior_auth": prior_auth,
            "audit": _audit_for_frontend(detail.get("audit_events") or []),
            "audit_events": detail.get("audit_events") or [],
            "provider": provider,
        }
    )


def _payload_for_frontend(
    canonical: dict[str, Any],
    payload_row: dict[str, Any],
    payload_text: str | None,
) -> dict[str, Any]:
    amount = canonical.get("amount") or {}
    line_items = canonical.get("line_items") or []
    return {
        "version": payload_row.get("version") or canonical.get("version") or "-",
        "format": payload_row.get("payload_type") or payload_row.get("standard") or "-",
        "hash": payload_row.get("sha256_hash") or "-",
        "generated": _display_dt(payload_row.get("generated_at") or payload_row.get("created_at")),
        "raw": payload_text or "",
        "lineItems": [
            {
                "code": item.get("code") or "-",
                "desc": item.get("description") or item.get("desc") or "-",
                "tooth": item.get("tooth") or "-",
                "surface": item.get("surface") or "-",
                "fee": float(item.get("net") or item.get("fee") or item.get("gross") or 0),
            }
            for item in line_items
        ],
        "totals": {
            "billed": float(amount.get("gross") or amount.get("billed") or 0),
            "insurance": float(amount.get("net") or 0),
            "patient": float(amount.get("patient_share") or 0),
        },
    }


def _issues_for_frontend(detail: dict[str, Any], report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    issues = report.get("issues") or detail.get("validation_issues") or []
    normalized = [
        {
            "code": issue.get("code") or "VALIDATION_ISSUE",
            "severity": str(issue.get("severity") or "INFO").upper(),
            "layer": issue.get("layer") or issue.get("check_type") or "Deterministic",
            "field": issue.get("field") or "-",
            "msg": issue.get("message") or issue.get("msg") or "Validation issue detected.",
            "suggestion": issue.get("suggestion") or issue.get("fix") or "Review before submission.",
        }
        for issue in issues
    ]
    return {
        "structural": [issue for issue in normalized if issue["code"].startswith("STRUCT")],
        "business": [issue for issue in normalized if not issue["code"].startswith("STRUCT")],
    }


def _eligibility_for_frontend(detail: dict[str, Any]) -> dict[str, Any]:
    row = detail.get("eligibility_result") or {}
    result = row.get("result") or row
    data = result.get("data") or {}
    benefits = data.get("benefit_summary") or row.get("benefit_summary") or {}
    if isinstance(benefits, dict):
        benefit_list = [{"k": str(key), "v": str(value)} for key, value in benefits.items()]
    else:
        benefit_list = []
    status = row.get("status") or result.get("status") or "Unknown"
    return {
        "status": "Active" if str(status).upper() in {"PASS", "CACHED_VALID", "ACTIVE"} else str(status),
        "memberId": row.get("member_id") or data.get("member_id") or "-",
        "coverageRef": row.get("coverage_ref") or data.get("coverage_ref") or "-",
        "checkedAgo": _relative_or_date(row.get("checked_at")),
        "validUntil": _display_dt(row.get("ttl_expires_at")),
        "voiFlag": "verified" if row.get("voi_ref") or data.get("voi_ref") else None,
        "benefits": benefit_list,
    }


def _prior_auth_for_frontend(detail: dict[str, Any]) -> dict[str, Any]:
    prior_auth = detail.get("prior_auth") or {}
    latest_request = prior_auth.get("latest_request") or {}
    latest_response = prior_auth.get("latest_response") or {}
    if not latest_request and not latest_response:
        return {"exists": False}
    status = latest_response.get("status") or latest_request.get("status") or "Pending"
    return {
        "exists": True,
        "ref": latest_response.get("pre_auth_ref") or latest_request.get("external_transaction_id") or latest_request.get("request_id"),
        "status": _title_status(status),
        "submitted": _relative_or_date(latest_request.get("submitted_at") or latest_request.get("created_at")),
        "response": latest_response.get("message") or latest_response.get("outcome"),
        "waitingSince": _display_dt(latest_request.get("waiting_since")),
        "nextPoll": _display_dt(latest_request.get("next_poll_at")),
        "attempts": latest_request.get("poll_attempt") or 0,
        "history": [
            {
                "ts": _display_dt(item.get("received_at") or item.get("created_at")),
                "note": item.get("message") or item.get("status") or "Payer response received.",
            }
            for item in prior_auth.get("responses", [])
        ],
    }


def _audit_for_frontend(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ts": _display_dt(event.get("ts") or event.get("created_at")),
            "agent": event.get("agent") or "Backend",
            "node": event.get("node") or "-",
            "type": event.get("event_type") or event.get("type") or "Event",
            "color": _audit_color(event.get("event_type") or event.get("type")),
        }
        for event in events
    ]


def _payload_text(payload_row: dict[str, Any], object_store: ObjectStoreInterface | None) -> str | None:
    object_uri = payload_row.get("object_uri")
    if not object_uri or object_store is None:
        return None
    try:
        return object_store.get_text(object_uri)
    except Exception:
        return None


def _route_value(route: dict[str, Any], key: str) -> Any:
    value = route.get(key)
    return str(value) if value is not None else None


def _frontend_status(status: Any) -> str:
    text = str(status or "NEEDS_REVIEW")
    return STATUS_TO_FRONTEND.get(text, text.lower() if text.isupper() else text)


def _format_amount(amount: dict[str, Any], detail: dict[str, Any]) -> str:
    currency = amount.get("currency") or detail.get("currency") or "AED"
    value = amount.get("net") or amount.get("gross") or detail.get("total_amount")
    if value is None:
        return "-"
    return f"{currency} {float(value):,.2f}"


def _relative_or_date(value: Any) -> str:
    return _display_dt(value) or "just now"


def _display_dt(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _title_status(value: Any) -> str:
    text = str(value or "Pending").replace("_", " ").lower()
    if text in {"approved", "active", "authorized", "authorised"}:
        return "Approved"
    if text in {"denied", "rejected", "declined"}:
        return "Denied"
    if text in {"expired"}:
        return "Expired"
    return "Pending"


def _audit_color(event_type: Any) -> str:
    text = str(event_type or "").upper()
    if "ERROR" in text or "FAIL" in text:
        return "#C22B2B"
    if "EXIT" in text or "COMPLETE" in text:
        return "#15883E"
    return "#1864AB"


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
