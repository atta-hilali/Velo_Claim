from __future__ import annotations

import time
from typing import Any

from velo_claim.context.adapters import AdapterInterface
from velo_claim.context.vendor_fhir_adapters import (
    FHIRAdapter,
    FHIRAdapterConfig,
    FHIRTokenManager,
    adapter_config_from_env,
    reference_id,
)
from velo_claim.storage.interfaces import CacheStoreInterface


class CacheBackedFHIRTokenManager(FHIRTokenManager):
    """FHIR token manager that shares OAuth tokens through Redis/cache."""

    def __init__(self, config: FHIRAdapterConfig, cache: CacheStoreInterface, cache_key: str) -> None:
        super().__init__(config)
        self.cache = cache
        self.cache_key = cache_key

    def invalidate(self) -> None:
        super().invalidate()
        self.cache.delete(self.cache_key)

    def access_token(self) -> str | None:
        if self.config.access_token:
            return self.config.access_token
        cached = self.cache.get(self.cache_key)
        if isinstance(cached, dict) and cached.get("access_token"):
            expires_at = float(cached.get("expires_at_epoch") or 0)
            if time.time() < expires_at - 60:
                self._access_token = str(cached["access_token"])
                self._expires_at_epoch = expires_at
                return self._access_token

        token = super().access_token()
        if token and self._expires_at_epoch:
            ttl = max(60, int(self._expires_at_epoch - time.time()))
            self.cache.set(
                self.cache_key,
                {"access_token": token, "expires_at_epoch": self._expires_at_epoch},
                ttl_seconds=ttl,
            )
        return token


class VendorFHIRAdapterBridge(AdapterInterface):
    """Bridge previous vendor FHIR adapter into the new AdapterInterface."""

    def __init__(
        self,
        adapter: FHIRAdapter | None = None,
        state: dict[str, Any] | None = None,
        cache: CacheStoreInterface | None = None,
    ) -> None:
        if adapter:
            self.adapter = adapter
            return
        state = state or {}
        config = adapter_config_from_env(state)
        token_manager = None
        if cache:
            token_manager = CacheBackedFHIRTokenManager(
                config,
                cache,
                _token_cache_key(state, config),
            )
        self.adapter = FHIRAdapter(config, token_manager=token_manager)

    def authenticate(self) -> str | None:
        return self.adapter.access_token()

    def fetch_encounter(self, encounter_id: str) -> dict[str, Any] | None:
        return self.adapter.read("Encounter", encounter_id)

    def fetch_patient(self, patient_id: str) -> dict[str, Any] | None:
        return self.adapter.read("Patient", patient_id)

    def fetch_coverage(self, patient_id: str, payer_id: str | None = None) -> dict[str, Any] | None:
        coverage = self.adapter.coverage_for_patient(patient_id)
        if coverage and payer_id:
            payor = coverage.get("payor", [{}])[0] if coverage.get("payor") else {}
            if payer_id not in {payor.get("identifier", {}).get("value"), payor.get("display")}:
                return None
        return coverage

    def fetch_practitioner(self, practitioner_id: str) -> dict[str, Any] | None:
        return self.adapter.read("Practitioner", practitioner_id)

    def fetch_organization(self, organization_id: str) -> dict[str, Any] | None:
        return self.adapter.read("Organization", organization_id)

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]:
        if "patient" in params:
            return self.adapter.search_patient_context(
                resource_type,
                patient_id=reference_id(params.get("patient")),
                encounter_id=reference_id(params.get("encounter")),
            )
        return self.adapter.search(resource_type, params)


def adapter_from_env(
    state: dict[str, Any] | None = None,
    cache: CacheStoreInterface | None = None,
) -> VendorFHIRAdapterBridge:
    return VendorFHIRAdapterBridge(state=state or {}, cache=cache)


def _token_cache_key(state: dict[str, Any], config: FHIRAdapterConfig) -> str:
    routing = state.get("routing_context", {}) if isinstance(state.get("routing_context"), dict) else {}
    provider_id = (
        state.get("provider_id")
        or routing.get("provider_license")
        or routing.get("facility_license")
        or config.client_id
        or config.adapter
        or "unknown_provider"
    )
    payer_id = state.get("payer_id") or routing.get("payer_id") or "unknown_payer"
    return f"token_cache:{provider_id}:{payer_id}"
