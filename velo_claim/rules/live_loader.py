from __future__ import annotations

from collections.abc import Callable
from typing import Any

from velo_claim.core.models import PayerRuleSet
from velo_claim.core.utils import normalize_code
from velo_claim.rules.interface import PayerRuleLoaderInterface
from velo_claim.rules.mock_loader import MockPayerRuleLoader
from velo_claim.storage.interfaces import CacheStoreInterface, RepositoryInterface


class PortalUnavailableError(RuntimeError):
    """Raised when a payer portal cannot provide the rule set."""


PayerRuleFetcher = Callable[[str, str], PayerRuleSet | dict[str, Any]]


class LivePayerRuleLoader(PayerRuleLoaderInterface):
    """Production-shaped payer rule loader with circuit-breaker fallback.

    The live portal fetcher is injected so each payer adapter can implement its
    own authentication and scraping/API details without changing validation
    nodes. If live fetching fails, the loader returns the latest persisted rule
    set as CACHED; if none exists, it falls back to the mock loader.
    """

    def __init__(
        self,
        *,
        repository: RepositoryInterface,
        cache: CacheStoreInterface,
        fetcher: PayerRuleFetcher | None = None,
        fallback_loader: PayerRuleLoaderInterface | None = None,
        breaker_ttl_seconds: int = 300,
        cache_ttl_seconds: int = 1800,
    ) -> None:
        self.repository = repository
        self.cache = cache
        self.fetcher = fetcher
        self.fallback_loader = fallback_loader or MockPayerRuleLoader()
        self.breaker_ttl_seconds = breaker_ttl_seconds
        self.cache_ttl_seconds = cache_ttl_seconds

    def load(self, payer_id: str, plan_id: str) -> PayerRuleSet:
        payer = normalize_code(payer_id) or "UNKNOWN"
        plan = normalize_code(plan_id) or "UNKNOWN"
        cache_key = f"payer_rules:{payer}:{plan}"
        cached = self.cache.get(cache_key)
        if cached:
            return _payer_rule_set_from_dict({**cached, "source": cached.get("source", "CACHED")})

        if not self.cache.get(_breaker_key(payer, plan)) and self.fetcher:
            try:
                live = _payer_rule_set_from_dict(self.fetcher(payer, plan))
                live = _copy_rule_set(live, source="LIVE")
                rule_dict = live.to_dict()
                self.repository.upsert_payer_rule_set(
                    payer,
                    plan,
                    {
                        "rule_set": rule_dict,
                        "eligibility_ttl_seconds": live.eligibility_ttl_seconds,
                    },
                )
                self.cache.set(cache_key, rule_dict, ttl_seconds=self.cache_ttl_seconds)
                return live
            except Exception as exc:
                self.cache.set(
                    _breaker_key(payer, plan),
                    {"open": True, "reason": str(exc)},
                    ttl_seconds=self.breaker_ttl_seconds,
                )

        persisted = self.repository.get_cached_payer_rule_set(payer, plan)
        if persisted:
            cached_rule = _copy_rule_set(_payer_rule_set_from_dict(persisted), source="CACHED")
            self.cache.set(cache_key, cached_rule.to_dict(), ttl_seconds=self.cache_ttl_seconds)
            return cached_rule

        return self.fallback_loader.load(payer, plan)


def _breaker_key(payer_id: str, plan_id: str) -> str:
    return f"circuit:payer_rules:{payer_id}:{plan_id}"


def _payer_rule_set_from_dict(value: PayerRuleSet | dict[str, Any]) -> PayerRuleSet:
    if isinstance(value, PayerRuleSet):
        return value
    return PayerRuleSet(
        payer_id=str(value.get("payer_id") or "UNKNOWN"),
        plan_id=str(value.get("plan_id") or "UNKNOWN"),
        eligibility_ttl_seconds=int(value.get("eligibility_ttl_seconds") or 3600),
        pa_required_cpt_codes=list(value.get("pa_required_cpt_codes") or []),
        bundling_rules=dict(value.get("bundling_rules") or {}),
        required_doc_types=dict(value.get("required_doc_types") or {}),
        submission_channel=str(value.get("submission_channel") or "MANUAL_PORTAL"),
        source=value.get("source") if value.get("source") in {"LIVE", "CACHED", "MOCK"} else "CACHED",
    )


def _copy_rule_set(rule_set: PayerRuleSet, *, source: str) -> PayerRuleSet:
    return PayerRuleSet(
        payer_id=rule_set.payer_id,
        plan_id=rule_set.plan_id,
        eligibility_ttl_seconds=rule_set.eligibility_ttl_seconds,
        pa_required_cpt_codes=list(rule_set.pa_required_cpt_codes),
        bundling_rules={key: list(value) for key, value in rule_set.bundling_rules.items()},
        required_doc_types={key: list(value) for key, value in rule_set.required_doc_types.items()},
        submission_channel=rule_set.submission_channel,
        source=source,
    )
