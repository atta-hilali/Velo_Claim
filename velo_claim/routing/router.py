from __future__ import annotations

from velo_claim.core.enums import ClaimStandard, Jurisdiction
from velo_claim.core.models import Route, RoutingContext
from velo_claim.core.utils import normalize_code, normalize_token
from velo_claim.routing.payer_registry import (
    infer_standard_for_payer,
    jurisdiction_for_payer,
    lookup_payer,
)
from velo_claim.storage.interfaces import RepositoryInterface


class ClaimRouter:
    """Turns routing facts into one persisted route decision."""

    def __init__(self, repository: RepositoryInterface) -> None:
        self.repository = repository

    def decide(self, claim_id: str, routing_context: RoutingContext | dict) -> Route:
        ctx = routing_context if isinstance(routing_context, RoutingContext) else RoutingContext(**routing_context)
        payer_record = lookup_payer(ctx.payer_id, ctx.payer_name)
        jurisdiction = self._resolve_jurisdiction(ctx)
        claim_standard = self._claim_standard(jurisdiction, ctx)
        route = Route(
            jurisdiction=jurisdiction,
            claim_standard=claim_standard,
            prior_auth_standard=claim_standard if claim_standard != ClaimStandard.ECLAIMLINK else ClaimStandard.ECLAIMLINK,
            eligibility_profile=self._eligibility_profile(ctx, jurisdiction),
            payer_rule_profile=self._payer_rule_profile(ctx, jurisdiction),
            submission_channel=self._submission_channel(claim_standard, ctx),
            evidence={
                "payer_id": ctx.payer_id,
                "payer_name": ctx.payer_name,
                "plan_id": ctx.plan_id,
                "facility_license_system": ctx.facility_license_system,
                "facility_license": ctx.facility_license,
                "payer_registry_match": payer_record,
            },
        )
        self.repository.put_route_decision(claim_id, route.to_dict())
        return route

    def _resolve_jurisdiction(self, ctx: RoutingContext) -> Jurisdiction:
        hint = normalize_token(ctx.jurisdiction_hint)
        license_text = normalize_token(f"{ctx.facility_license_system} {ctx.facility_license}")
        payer = normalize_token(f"{ctx.payer_id} {ctx.payer_name}")
        registry_jurisdiction = jurisdiction_for_payer(ctx.payer_id, ctx.payer_name)

        if hint in {"ksa", "saudi", "saudiarabia"} or "nphies" in license_text or "tawuniya" in payer:
            return Jurisdiction.KSA
        if hint in {"dubai", "dha"} or "dha" in license_text or "dxb" in license_text:
            return Jurisdiction.DUBAI
        if hint in {"abudhabi", "abudhabi", "doh"} or "doh" in license_text or normalize_code(ctx.facility_license).startswith("MF"):
            return Jurisdiction.ABU_DHABI
        if registry_jurisdiction:
            try:
                return Jurisdiction(registry_jurisdiction)
            except ValueError:
                pass
        return Jurisdiction.UNKNOWN

    def _claim_standard(self, jurisdiction: Jurisdiction, ctx: RoutingContext) -> ClaimStandard:
        registry_standard = infer_standard_for_payer(
            ctx.payer_id,
            ctx.payer_name,
            jurisdiction=jurisdiction.value,
        )
        if registry_standard:
            try:
                return ClaimStandard(registry_standard)
            except ValueError:
                pass
        return {
            Jurisdiction.KSA: ClaimStandard.NPHIES,
            Jurisdiction.DUBAI: ClaimStandard.ECLAIMLINK,
            Jurisdiction.ABU_DHABI: ClaimStandard.SHAFAFIYA,
        }.get(jurisdiction, ClaimStandard.CANONICAL)

    def _eligibility_profile(self, ctx: RoutingContext, jurisdiction: Jurisdiction) -> str:
        payer = normalize_token(ctx.payer_name or ctx.payer_id)
        if jurisdiction == Jurisdiction.ABU_DHABI and ("daman" in payer or normalize_code(ctx.payer_id) == "A001"):
            return "DAMAN_VOI"
        return f"{jurisdiction}_STANDARD"

    def _payer_rule_profile(self, ctx: RoutingContext, jurisdiction: Jurisdiction) -> str:
        payer = normalize_code(ctx.payer_id)
        plan = normalize_code(ctx.plan_id)
        return f"{jurisdiction}_{payer}_{plan}"

    def _submission_channel(self, standard: ClaimStandard, ctx: RoutingContext) -> str:
        if standard == ClaimStandard.NPHIES:
            return "NPHIES_GATEWAY"
        if standard == ClaimStandard.SHAFAFIYA:
            return "SHAFAFIYA_GATEWAY_OR_PORTAL"
        if standard == ClaimStandard.ECLAIMLINK:
            return "ECLAIMLINK_GATEWAY_OR_PORTAL"
        return "MANUAL_PORTAL"
