from __future__ import annotations

from typing import Any, Callable

from velo_claim.core.enums import CallbackSource
from velo_claim.fallback.callbacks import CallbackProcessor
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.storage.interfaces import CacheStoreInterface, ObjectStoreInterface, RepositoryInterface


def receive_payer_webhook(
    *,
    claim_id: str,
    body: dict[str, Any],
    repository: RepositoryInterface,
    cache: CacheStoreInterface,
    checkpoint_store: MemoryCheckpointStore,
    object_store: ObjectStoreInterface | None = None,
    callback_state: dict[str, Any] | None = None,
    resume_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    result = CallbackProcessor(repository=repository, cache=cache, object_store=object_store).process(
        claim_id=claim_id,
        source=CallbackSource.WEBHOOK,
        raw_payload=body,
    )
    if result["status"] != "accepted":
        return result
    if callback_state:
        resumed_state = checkpoint_store.inject_node_result(
            thread_id=callback_state["thread_id"],
            checkpoint_id=callback_state["checkpoint_id"],
            resume_node=callback_state["resume_node"],
            result=body,
        )
        if resume_callback:
            resume_callback(resumed_state)
    return result


def build_fastapi_router(
    repository: RepositoryInterface,
    cache: CacheStoreInterface,
    checkpoint_store: MemoryCheckpointStore,
    object_store: ObjectStoreInterface | None = None,
    resume_callback: Callable[[dict[str, Any]], Any] | None = None,
):
    try:
        from fastapi import APIRouter
    except ImportError as exc:
        raise RuntimeError("Install fastapi to expose payer webhook routes: pip install fastapi") from exc

    router = APIRouter()

    @router.post("/webhooks/payer/{claim_id}")
    async def payer_webhook(claim_id: str, body: dict[str, Any]):
        return receive_payer_webhook(
            claim_id=claim_id,
            body=body,
            repository=repository,
            cache=cache,
            checkpoint_store=checkpoint_store,
            object_store=object_store,
            resume_callback=resume_callback,
        )

    return router
