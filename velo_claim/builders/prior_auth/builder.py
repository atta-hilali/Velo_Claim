from __future__ import annotations

from velo_claim.builders.prior_auth.canonical import build_pa_canonical_form
from velo_claim.builders.prior_auth.eclaimlink import EClaimLinkPABuilder
from velo_claim.builders.prior_auth.nphies import NphiesPABuilder
from velo_claim.builders.prior_auth.shafafiya import ShafafiyaPABuilder
from velo_claim.core.enums import ClaimStandard
from velo_claim.core.models import CanonicalState, payload_extension
from velo_claim.core.utils import sha256_text
from velo_claim.storage.interfaces import ObjectStoreInterface, RepositoryInterface


class PAClaimBuilderModule:
    def __init__(self, *, repository: RepositoryInterface, object_store: ObjectStoreInterface) -> None:
        self.repository = repository
        self.object_store = object_store
        self._builders = {
            ClaimStandard.NPHIES: NphiesPABuilder(),
            ClaimStandard.SHAFAFIYA: ShafafiyaPABuilder(),
            ClaimStandard.ECLAIMLINK: EClaimLinkPABuilder(),
        }

    def build(self, state: CanonicalState, required_codes: list[str]) -> CanonicalState:
        route = state.get("route", {})
        standard = ClaimStandard(route.get("prior_auth_standard") or route.get("claim_standard") or ClaimStandard.SHAFAFIYA)
        builder = self._builders[standard]
        form = build_pa_canonical_form(state, required_codes)
        payload = builder.build(form)
        version = int(state.get("pa_payload_version") or 0) + 1
        ext = payload_extension(builder.content_type)
        uri = self.object_store.put_text(
            f"claims/{form.claim_id}/prior_auth/pa_payloads/{version}/payload.{ext}",
            payload,
            content_type=builder.content_type,
        )
        self.repository.insert_pa_payload(
            form.claim_id,
            version,
            {
                "standard": standard,
                "payload_type": builder.content_type,
                "object_uri": uri,
                "sha256_hash": sha256_text(payload),
                "status": "DRAFT_BUILT",
            },
        )
        return {
            **state,
            "pa_payload": payload,
            "pa_payload_uri": uri,
            "pa_payload_type": builder.content_type,
            "pa_payload_version": version,
        }
