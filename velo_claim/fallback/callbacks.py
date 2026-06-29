from __future__ import annotations

from typing import Any

from velo_claim.core.enums import CallbackSource
from velo_claim.core.utils import sha256_obj
from velo_claim.storage.interfaces import (
    CacheStoreInterface,
    DuplicateRecordError,
    ObjectStoreInterface,
    RepositoryInterface,
)


class CallbackProcessor:
    """Shared webhook/poll callback idempotency guard."""

    def __init__(
        self,
        *,
        repository: RepositoryInterface,
        cache: CacheStoreInterface,
        object_store: ObjectStoreInterface | None = None,
    ) -> None:
        self.repository = repository
        self.cache = cache
        self.object_store = object_store

    def process(
        self,
        *,
        claim_id: str,
        source: CallbackSource,
        raw_payload: dict[str, Any],
        lock_already_acquired: bool = False,
    ) -> dict[str, Any]:
        transaction_ref = raw_payload.get("transaction_ref") or raw_payload.get("reference") or sha256_obj(raw_payload)
        idempotency_key = sha256_obj({"claim_id": claim_id, "callback": transaction_ref})
        lock_key = f"callback_lock:{claim_id}"
        if not lock_already_acquired and not self.cache.set(lock_key, "1", ttl_seconds=30, nx=True):
            return {"status": "duplicate_ignored", "idempotency_key": idempotency_key}
        try:
            self.repository.insert_callback_event(
                claim_id,
                idempotency_key,
                {"source": source, "raw_payload": raw_payload},
            )
            if self.object_store:
                self.object_store.put_text(
                    f"claims/{claim_id}/callbacks/{idempotency_key}.json",
                    __import__("json").dumps(raw_payload, indent=2, default=str),
                    content_type="application/json",
                )
        except DuplicateRecordError:
            return {"status": "already_processed", "idempotency_key": idempotency_key}
        finally:
            if not lock_already_acquired:
                self.cache.delete(lock_key)
        return {"status": "accepted", "idempotency_key": idempotency_key}
