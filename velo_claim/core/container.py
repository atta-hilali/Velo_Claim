from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from velo_claim.core.env import load_env_file
from velo_claim.submission.store import MemorySubmissionStore, PostgresSubmissionStore
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.kg.json_client import JsonKnowledgeGraphClient
from velo_claim.kg.mock import MockNeo4jClient
from velo_claim.kg.neo4j import Neo4jKnowledgeGraphClient
from velo_claim.payers.factory import build_payer_transport_registry
from velo_claim.payers.interface import PayerTransportRegistry
from velo_claim.rules.file_loader import FilePayerRuleLoader
from velo_claim.rules.http_fetcher import HttpPayerRuleFetcher
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
    kg_client: Neo4jClientInterface
    payer_rule_loader: PayerRuleLoaderInterface
    payer_transports: PayerTransportRegistry
    submission_store: MemorySubmissionStore | PostgresSubmissionStore = field(default_factory=MemorySubmissionStore)


def build_default_container() -> ServiceContainer:
    """Build a local runnable container with production-shaped interfaces."""

    repository = InMemoryRepository()
    object_store = InMemoryObjectStore()
    cache = InMemoryCacheStore()
    kg_client = MockNeo4jClient()
    payer_rule_loader = MockPayerRuleLoader()
    payer_transports = build_payer_transport_registry()
    return ServiceContainer(
        repository=repository,
        object_store=object_store,
        cache=cache,
        kg_client=kg_client,
        payer_rule_loader=payer_rule_loader,
        payer_transports=payer_transports,
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
    project_root = Path(__file__).resolve().parents[2]
    rules_path = _resolve_project_path(
        os.getenv("PAYER_RULES_PATH", "./data/payer_rules/default_rules.json"), project_root
    )
    payer_registry_path = _resolve_project_path(
        os.getenv("PAYER_REGISTRY_PATH", "./data/payers/phase1_payers.json"), project_root
    )
    file_rules = FilePayerRuleLoader(rules_path, payer_registry_path)
    http_fetcher = HttpPayerRuleFetcher.from_env()
    return ServiceContainer(
        repository=repository,
        object_store=object_store,
        cache=cache,
        kg_client=_build_kg_client(project_root),
        submission_store=PostgresSubmissionStore(repository),
        payer_rule_loader=LivePayerRuleLoader(
            repository=repository,
            cache=cache,
            fetcher=http_fetcher,
            fallback_loader=file_rules,
        ),
        payer_transports=build_payer_transport_registry(),
    )


def _resolve_project_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def _build_kg_client(project_root: Path) -> Neo4jClientInterface:
    backend = os.getenv("VALIDATION_KG_BACKEND", "").strip().lower()
    if backend == "neo4j":
        client = Neo4jKnowledgeGraphClient.from_env()
        if os.getenv("NEO4J_VERIFY_CONNECTIVITY", "true").lower() in {"1", "true", "yes"}:
            client.verify_connectivity(raise_on_error=False)
        return client
    if backend == "json":
        path = _resolve_project_path(
            os.getenv("VALIDATION_KG_PATH", "./data/coding_knowledge_graph.json"), project_root
        )
        return JsonKnowledgeGraphClient(path)
    if backend == "mock":
        return MockNeo4jClient()
    raise ValueError(
        "Production requires an explicit VALIDATION_KG_BACKEND value: neo4j, json, or mock. "
        "Use neo4j on DGX; mock and json are intended only for explicit development/test runtimes."
    )
