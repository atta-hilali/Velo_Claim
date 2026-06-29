from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from velo_claim.core.enums import PayloadStatus
from velo_claim.core.models import CallbackState, CanonicalState
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.storage.interfaces import CacheStoreInterface


BACKOFF_SCHEDULE = [30, 60, 120, 300, 900, 1800, 3600]


def enter_waiting_for_payer(
    *,
    state: CanonicalState,
    cache: CacheStoreInterface,
    agent: str,
    node: str,
    thread_id: str,
    resume_node: str,
    checkpoint_id: str | None = None,
    checkpoint_store: MemoryCheckpointStore | None = None,
) -> CanonicalState:
    checkpoint_id = checkpoint_id or (checkpoint_store.put(thread_id, state) if checkpoint_store else str(uuid4()))
    cache.set(f"lg_checkpoint:{thread_id}:{checkpoint_id}", checkpoint_id, ttl_seconds=7200)
    job_id = str(uuid4())
    now = datetime.now(UTC)
    next_poll_at = now + timedelta(seconds=BACKOFF_SCHEDULE[0])
    job = {
        "job_id": job_id,
        "claim_id": state.get("claim", {}).get("claim_id"),
        "agent": agent,
        "node": node,
        "thread_id": thread_id,
        "checkpoint_id": checkpoint_id,
        "attempt": 0,
        "backoff_schedule": BACKOFF_SCHEDULE,
        "next_poll_at": next_poll_at.isoformat(),
        "waiting_since": now.isoformat(),
        "status": "WAITING",
        "resume_node": resume_node,
        "payer_id": state.get("routing_context", {}).get("payer_id"),
    }
    cache.set(f"bg_job:{job_id}", job, ttl_seconds=21600)
    callback_state = CallbackState(
        bg_job_id=job_id,
        thread_id=thread_id,
        checkpoint_id=checkpoint_id,
        poll_attempt=0,
        next_poll_at=job["next_poll_at"],
        waiting_since=job["waiting_since"],
        resume_node=resume_node,
    )
    return {
        **state,
        "payload_status": PayloadStatus.WAITING_FOR_PAYER,
        "callback_state": callback_state.to_dict(),
    }
