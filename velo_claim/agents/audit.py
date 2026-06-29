from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any

from velo_claim.core.utils import utc_now
from velo_claim.storage.interfaces import ObjectStoreInterface, RepositoryInterface


NodeFn = Callable[[dict[str, Any]], dict[str, Any]]


def audited_node(
    *,
    agent: str,
    node: str,
    fn: NodeFn,
    repository: RepositoryInterface,
    object_store: ObjectStoreInterface | None = None,
) -> NodeFn:
    """Wrap a LangGraph node with NODE_ENTER/NODE_EXIT/NODE_ERROR audit events."""

    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        claim_id = _claim_id(state)
        enter_payload = {"input_snapshot": _snapshot(state)}
        _write_audit(repository, object_store, claim_id, agent, node, "NODE_ENTER", enter_payload)
        started = perf_counter()
        try:
            result = fn(state)
            duration_ms = round((perf_counter() - started) * 1000, 2)
            output_snapshot = _snapshot(result)
            before_errors = len(state.get("errors", []))
            after_errors = len(result.get("errors", []))
            exit_payload = {
                "input_snapshot": enter_payload["input_snapshot"],
                "output_snapshot": output_snapshot,
                "duration_ms": duration_ms,
                "errors_added": result.get("errors", [])[before_errors:after_errors],
            }
            _write_audit(repository, object_store, _claim_id(result) or claim_id, agent, node, "NODE_EXIT", exit_payload)
            return result
        except Exception as exc:
            duration_ms = round((perf_counter() - started) * 1000, 2)
            error_payload = {
                "input_snapshot": enter_payload["input_snapshot"],
                "duration_ms": duration_ms,
                "error": str(exc),
            }
            _write_audit(repository, object_store, claim_id, agent, node, "NODE_ERROR", error_payload)
            raise

    return wrapped


def _write_audit(
    repository: RepositoryInterface,
    object_store: ObjectStoreInterface | None,
    claim_id: str | None,
    agent: str,
    node: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    event = {
        "agent": agent,
        "node": node,
        "event_type": event_type,
        "payload": payload,
        "ts": utc_now(),
    }
    repository.insert_audit_event(claim_id or "UNKNOWN_CLAIM", event)
    if object_store:
        safe_claim = claim_id or "UNKNOWN_CLAIM"
        key = f"claims/{safe_claim}/audit/{event['ts']}-{agent}-{node}-{event_type}.json"
        object_store.put_text(key, __import__("json").dumps(event, indent=2, default=str), content_type="application/json")


def _claim_id(state: dict[str, Any]) -> str | None:
    return (
        state.get("claim", {}).get("claim_id")
        or state.get("claim_id")
        or state.get("canonical_claim", {}).get("claim_id")
    )


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": _claim_id(state),
        "payload_status": state.get("payload_status"),
        "claim_format": state.get("claim_format"),
        "jurisdiction": state.get("jurisdiction"),
        "route": state.get("route", {}),
        "payload_version": state.get("payload_version"),
        "pa_payload_version": state.get("pa_payload_version"),
        "error_count": len(state.get("errors", [])),
        "warning_count": len(state.get("warnings", [])),
    }
