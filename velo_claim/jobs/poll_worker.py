from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from velo_claim.core.enums import CallbackSource, PayloadStatus
from velo_claim.core.models import ClaimError
from velo_claim.core.enums import Severity
from velo_claim.core.utils import utc_now
from velo_claim.fallback.callbacks import CallbackProcessor
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.storage.interfaces import CacheStoreInterface, ObjectStoreInterface, RepositoryInterface


class PayerStatusAdapter(Protocol):
    def get_status(self, job: dict[str, Any]) -> dict[str, Any]: ...


def due_bg_jobs(cache: CacheStoreInterface, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now(UTC)
    jobs = []
    key_iter = cache.keys("bg_job:") if hasattr(cache, "keys") else []
    for key in key_iter:
        job = cache.get(key)
        if not isinstance(job, dict):
            continue
        next_poll_at = _parse_dt(job.get("next_poll_at"))
        if next_poll_at and next_poll_at <= current:
            jobs.append(job)
    return jobs


def process_poll_job(
    *,
    job: dict[str, Any],
    cache: CacheStoreInterface,
    repository: RepositoryInterface,
    payer_adapter: PayerStatusAdapter,
    checkpoint_store: MemoryCheckpointStore,
    object_store: ObjectStoreInterface | None = None,
    resume_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    claim_id = job["claim_id"]
    payer_id = job.get("payer_id") or "UNKNOWN"
    callback_lock = f"callback_lock:{claim_id}"
    poll_lock = f"poll_lock:{claim_id}:{payer_id}"
    if not cache.set(callback_lock, "1", ttl_seconds=30, nx=True):
        return {"status": "callback_already_processing"}
    if not cache.set(poll_lock, "1", ttl_seconds=300, nx=True):
        cache.delete(callback_lock)
        return {"status": "poll_already_processing"}

    try:
        response = payer_adapter.get_status(job)
        status = str(response.get("status") or response.get("outcome") or "").lower()
        if status in {"queued", "pending", "pended", "partial"}:
            return _reschedule_or_escalate(job, cache, repository, object_store)

        callback_result = CallbackProcessor(repository=repository, cache=cache, object_store=object_store).process(
            claim_id=claim_id,
            source=CallbackSource.POLL,
            raw_payload=response,
            lock_already_acquired=True,
        )
        if callback_result["status"] == "accepted":
            resumed_state = checkpoint_store.inject_node_result(
                thread_id=job["thread_id"],
                checkpoint_id=job["checkpoint_id"],
                resume_node=job["resume_node"],
                result=response,
            )
            if resume_callback:
                resume_callback(resumed_state)
            cache.delete(f"bg_job:{job['job_id']}")
        return {"status": "processed", "callback": callback_result}
    finally:
        cache.delete(poll_lock)
        cache.delete(callback_lock)


def _reschedule_or_escalate(
    job: dict[str, Any],
    cache: CacheStoreInterface,
    repository: RepositoryInterface,
    object_store: ObjectStoreInterface | None = None,
) -> dict[str, Any]:
    waiting_since = _parse_dt(job.get("waiting_since")) or datetime.now(UTC)
    if datetime.now(UTC) - waiting_since > timedelta(hours=24):
        claim_id = job["claim_id"]
        error = ClaimError(
            code="PAYER_RESPONSE_TIMEOUT",
            severity=Severity.ERROR,
            check_type="CALLBACK",
            field="callback_state.waiting_since",
            message="Payer response was not received within 24 hours.",
            suggestion="Escalate to RCM review or contact the payer support channel.",
            agent=job.get("agent", "UnknownAgent"),
            node=job.get("node", "poll_worker"),
        )
        audit_event = {
            "agent": "PollWorker",
            "node": "process_poll_job",
            "event_type": "ESCALATED_TIMEOUT",
            "payload": {"error": error.to_dict(), "payload_status": PayloadStatus.NEEDS_REVIEW},
            "ts": utc_now(),
        }
        repository.insert_audit_event(claim_id, audit_event)
        if object_store:
            object_store.put_text(
                f"claims/{claim_id}/audit/{audit_event['ts']}-PollWorker-process_poll_job-ESCALATED_TIMEOUT.json",
                json.dumps(audit_event, indent=2, default=str),
                content_type="application/json",
            )
        cache.delete(f"bg_job:{job['job_id']}")
        return {"status": "escalated_timeout", "error": error.to_dict()}

    schedule = job.get("backoff_schedule", [30, 60, 120, 300, 900, 1800, 3600])
    attempt = int(job.get("attempt", 0)) + 1
    delay = schedule[attempt] if attempt < len(schedule) else schedule[-1]
    job = {**job, "attempt": attempt, "next_poll_at": (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()}
    cache.set(f"bg_job:{job['job_id']}", job, ttl_seconds=21600)
    return {"status": "rescheduled", "attempt": attempt, "next_poll_at": job["next_poll_at"]}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
