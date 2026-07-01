from __future__ import annotations

import os
from dataclasses import dataclass

from velo_claim.core.env import load_env_file
from velo_claim.kg.mock import MockNeo4jClient
from velo_claim.rules.interface import PayerRuleLoaderInterface
from velo_claim.rules.live_loader import LivePayerRuleLoader
from velo_claim.rules.mock_loader import MockPayerRuleLoader
from velo_claim.storage.interfaces import CacheStoreInterface, ObjectStoreInterface, RepositoryInterface
from velo_claim.storage.memory import InMemoryCacheStore, InMemoryObjectStore, InMemoryRepository
from velo_claim.storage.object_store import S3ObjectStore
from velo_claim.storage.postgres import PostgresRepository
from velo_claim.storage.redis_cache import RedisCacheStore


@dataclass(slots=True)
class ServiceContainer:
    repository: RepositoryInterface
    object_store: ObjectStoreInterface
    cache: CacheStoreInterface
    kg_client: MockNeo4jClient
    payer_rule_loader: PayerRuleLoaderInterface


def build_default_container() -> ServiceContainer:
    """Build a local runnable container with production-shaped interfaces."""

    repository = InMemoryRepository()
    object_store = InMemoryObjectStore()
    cache = InMemoryCacheStore()
    kg_client = MockNeo4jClient()
    payer_rule_loader = MockPayerRuleLoader()
    return ServiceContainer(
        repository=repository,
        object_store=object_store,
        cache=cache,
        kg_client=kg_client,
        payer_rule_loader=payer_rule_loader,
    )


def build_container_from_env() -> ServiceContainer:
    """Build local or production adapters according to environment.

    VELO_CLAIM_STORAGE=memory keeps the zero-dependency default. Set
    VELO_CLAIM_STORAGE=production to use PostgreSQL, S3/MinIO, and Redis.
    """

    load_env_file()
    if os.getenv("VELO_CLAIM_STORAGE", "memory").lower() != "production":
        return build_default_container()
    repository = PostgresRepository()
    object_store = S3ObjectStore()
    cache = RedisCacheStore()
    return ServiceContainer(
        repository=repository,
        object_store=object_store,
        cache=cache,
        kg_client=MockNeo4jClient(),
        payer_rule_loader=LivePayerRuleLoader(
            repository=repository,
            cache=cache,
            fetcher=None,
            fallback_loader=MockPayerRuleLoader(),
        ),
    )
