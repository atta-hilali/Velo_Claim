from __future__ import annotations

from typing import Any

from velo_claim.core.utils import sha256_text
from velo_claim.storage.interfaces import CacheStoreInterface


def idempotency_key(*parts: object) -> str:
    return sha256_text("|".join(str(part or "") for part in parts))


class IdempotencyGuard:
    def __init__(self, cache: CacheStoreInterface, ttl_seconds: int = 86400) -> None:
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    def acquire(self, key: str) -> bool:
        return self.cache.set(f"idempotency:{key}", "processing", ttl_seconds=self.ttl_seconds, nx=True)

    def complete(self, key: str, result: Any) -> None:
        self.cache.set(f"idempotency:{key}", result, ttl_seconds=self.ttl_seconds)

    def get(self, key: str) -> Any | None:
        return self.cache.get(f"idempotency:{key}")

    def release(self, key: str) -> None:
        self.cache.delete(f"idempotency:{key}")
