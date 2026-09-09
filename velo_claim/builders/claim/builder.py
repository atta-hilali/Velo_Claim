from __future__ import annotations

from velo_claim.builders.claim.canonical import build_canonical_claim
from velo_claim.builders.claim.eclaimlink import EClaimLinkClaimBuilder
from velo_claim.builders.claim.nphies import NphiesClaimBuilder
from velo_claim.builders.claim.shafafiya import ShafafiyaClaimBuilder
from velo_claim.core.enums import ClaimStandard, PayloadStatus
from velo_claim.core.models import CanonicalState, RoutingContext, payload_extension
from velo_claim.core.utils import sha256_text
from velo_claim.kg.interface import Neo4jClientInterface
from velo_claim.rules.interface import PayerRuleLoaderInterface
from velo_claim.storage.interfaces import ObjectStoreInterface, RepositoryInterface


class ClaimBuilderModule:
    """Reusable claim builder: source context -> canonical claim -> wire payload."""

    def __init__(
        self,
        *,
        repository: RepositoryInterface,
        object_store: ObjectStoreInterface,
        kg_client: Neo4jClientInterface,
        payer_rule_loader: PayerRuleLoaderInterface,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.kg_client = kg_client
        self.payer_rule_loader = payer_rule_loader
        self._builders = {
            ClaimStandard.NPHIES: NphiesClaimBuilder(),
            ClaimStandard.SHAFAFIYA: ShafafiyaClaimBuilder(),
            ClaimStandard.ECLAIMLINK: EClaimLinkClaimBuilder(),
        }

    def build(self, state: CanonicalState) -> CanonicalState:
        claim_id = state.get("claim", {}).get("claim_id") or state.get("claim_id")
        routing_context = RoutingContext(**state.get("routing_context", {}))
        route = state.get("route", {})
        standard = ClaimStandard(route.get("claim_standard") or state.get("claim_format") or ClaimStandard.CANONICAL)
        payer_rules = self.payer_rule_loader.load(routing_context.payer_id, routing_context.plan_id)
        canonical_claim = build_canonical_claim(
            claim_id=claim_id,
            source_context=state["source_context"],
            routing_context=routing_context,
            payer_rules=payer_rules,
            kg_client=self.kg_client,
        )
        existing_pre_auth_ref = state.get("canonical_claim", {}).get("pre_auth_ref") or state.get("pre_auth_ref")
        if existing_pre_auth_ref:
            canonical_claim["pre_auth_ref"] = existing_pre_auth_ref
        existing_eligibility_ref = (
            state.get("canonical_claim", {}).get("payer", {}).get("eligibility_ref")
            or state.get("eligibility_ref")
        )
        if existing_eligibility_ref:
            canonical_claim["payer"]["eligibility_ref"] = existing_eligibility_ref
        return self.build_payload_from_canonical(
            state,
            canonical_claim,
            rebuild_reason=state.get("rebuild_reason"),
            created_by_agent=state.get("created_by_agent") or "ClaimPreparationAgent",
        )

    def build_payload_from_canonical(
        self,
        state: CanonicalState,
        canonical_claim: dict,
        *,
        rebuild_reason: str | None = None,
        created_by_agent: str | None = None,
        persist: bool = True,
    ) -> CanonicalState:
        """Serialize an existing canonical claim without reconstructing it.

        Correction workflows use ``persist=False`` and commit the returned
        payload metadata together with the new claim version in one database
        transaction after human approval.
        """

        routing_context = RoutingContext(**state.get("routing_context", {}))
        route = state.get("route", {})
        standard = ClaimStandard(
            route.get("claim_standard")
            or state.get("claim_format")
            or ClaimStandard.CANONICAL
        )
        canonical_claim = dict(canonical_claim)
        claim_id = canonical_claim["claim_id"]
        if persist:
            self.repository.upsert_claim(
                claim_id,
                {
                    "claim_id": claim_id,
                    "status": PayloadStatus.DRAFT_BUILDING,
                    "jurisdiction": route.get("jurisdiction"),
                    "payer_id": routing_context.payer_id,
                    "provider_id": canonical_claim["provider"].get("id") or routing_context.provider_license,
                    "patient_id": canonical_claim["patient"].get("id"),
                },
            )
        payload = self._serialize(standard, canonical_claim)
        payload_type = self._builders[standard].content_type if standard in self._builders else "application/json"
        version = int(state.get("payload_version") or 0) + 1
        ext = payload_extension(payload_type)
        sha256_hash = sha256_text(payload)
        object_uri = self.object_store.put_text(
            f"claims/{claim_id}/versions/{version}/payload-{sha256_hash[:16]}.{ext}",
            payload,
            content_type=payload_type,
        )
        payload_data = {
            "standard": standard,
            "payload_type": payload_type,
            "object_uri": object_uri,
            "sha256_hash": sha256_hash,
            "status": PayloadStatus.DRAFT_BUILT,
            "generated_by_agent": created_by_agent,
        }
        version_data = {
            "canonical_claim": canonical_claim,
            "route": route,
            "source_context": state.get("source_context", {}),
            "routing_context": state.get("routing_context", {}),
            "parent_version": int(state.get("payload_version") or 0) or None,
            "rebuild_reason": rebuild_reason,
            "rebuild_attempt": int(state.get("rebuild_attempt_count") or 0),
            "created_by_agent": created_by_agent,
            "correction_cycle_id": state.get("correction_cycle_id"),
        }
        if persist:
            self.repository.insert_claim_version(claim_id, version, version_data)
            self.repository.insert_claim_payload(claim_id, version, payload_data)
        return {
            **state,
            "claim": {"claim_id": claim_id, "version": version},
            "canonical_claim": canonical_claim,
            "claim_payload": payload,
            "claim_payload_uri": object_uri,
            "claim_payload_hash": sha256_hash,
            "claim_payload_type": payload_type,
            "payload_status": PayloadStatus.DRAFT_BUILT,
            "payload_version": version,
            "payload_rebuild_required": False,
            "claim_format": standard,
            "jurisdiction": route.get("jurisdiction"),
            "next_agent": "ClaimValidationAgent",
            "_claim_version_data": version_data,
            "_claim_payload_data": payload_data,
        }

    def _serialize(self, standard: ClaimStandard, canonical_claim: dict) -> str:
        if standard not in self._builders:
            raise ValueError(f"No claim builder registered for {standard}.")
        return self._builders[standard].build(canonical_claim)
