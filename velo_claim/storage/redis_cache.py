from __future__ import annotations

import json
import os
from typing import Any

from velo_claim.storage.interfaces import CacheStoreInterface


class RedisCacheStore(CacheStoreInterface):
    """Redis-backed cache/lock implementation."""

    def __init__(self, url: str | None = None) -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Install redis to use RedisCacheStore: pip install redis") from exc
        self.client = redis.Redis.from_url(url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

    def set(self, key: str, value: Any, ttl_seconds: int | None = None, nx: bool = False) -> bool:
        encoded = json.dumps(value, default=str)
        return bool(self.client.set(name=key, value=encoded, ex=ttl_seconds, nx=nx))

    def get(self, key: str) -> Any | None:
        value = self.client.get(key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def delete(self, key: str) -> None:
        self.client.delete(key)

    def keys(self, pattern: str) -> list[str]:
        match = pattern if "*" in pattern else f"{pattern}*"
        return [str(key) for key in self.client.scan_iter(match=match)]
