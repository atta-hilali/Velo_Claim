from __future__ import annotations

from velo_claim.builders.prior_auth.canonical import build_pa_canonical_form
from velo_claim.builders.prior_auth.eclaimlink import EClaimLinkPABuilder
import uuid
from velo_claim.builders.prior_auth.nphies import NphiesPABuilder
from velo_claim.builders.prior_auth.shafafiya import ShafafiyaPABuilder
from velo_claim.core.enums import ClaimStandard
from velo_claim.core.models import CanonicalState, payload_extension
from velo_claim.core.utils import sha256_text
from velo_claim.standards.shafafiya import bounded_identifier
from velo_claim.storage.interfaces import ObjectStoreInterface, RepositoryInterface
from velo_claim.validation.payload_validators import PayloadValidator


class PAClaimBuilderModule:
    def __init__(
        self,
        *,
        repository: RepositoryInterface,
        object_store: ObjectStoreInterface,
        submission_store=None,
        payload_validator: PayloadValidator | None = None,
    ) -> None:
        self.submission_store = submission_store
        self.repository = repository
        self.object_store = object_store
        self.payload_validator = payload_validator or PayloadValidator()
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

        claim_id = form.claim_id

        linked_claim_id = claim_id if (claim_id and self.repository.get_claim_detail(claim_id)) else None
        request_id = str(uuid.uuid4())
        display_id = f"PA-{uuid.uuid4().hex[:12].upper()}"
        form.request_identifier = bounded_identifier(
            f"{form.facility.get('license') or 'FAC'}-PA-{uuid.uuid4().hex}"
        )
        payload = builder.build(form)
        _, validation = self.payload_validator.validate_prior_request(
            payload=payload,
            payload_type=builder.content_type,
            route={**route, "prior_auth_standard": standard},
        )
        if not validation.passes:
            messages = "; ".join(issue.message for issue in validation.issues)
            raise ValueError(f"Generated prior-authorization payload is invalid: {messages}")
        version = int(state.get("pa_payload_version") or 0) + 1
        ext = payload_extension(builder.content_type)

        prefix = f"prior_auth/{claim_id}" if linked_claim_id else "prior_auth/unlinked"
        uri = self.object_store.put_text(
            f"{prefix}/{display_id}/payload.{ext}",
            payload,
            content_type=builder.content_type,
        )

        self.repository.insert_prior_auth_request(
            linked_claim_id,
            {
                "request_id": request_id,
                "display_id": display_id,
                "standard": standard,
                "payload_type": builder.content_type,
                "object_uri": uri,
                "status": "REQUIRED_MISSING",
                "payer_id": form.payer_id,
                "plan_id": form.plan_id,
                "service_date": form.service_date,
                "required_codes": required_codes,
                "sha256_hash": sha256_text(payload),
                "payload_validation": validation.to_dict(),
            },
        )

        if linked_claim_id:
            self.repository.insert_pa_payload(
                linked_claim_id,
                version,
                {
                    "request_id": request_id,
                    "standard": standard,
                    "payload_type": builder.content_type,
                    "object_uri": uri,
                    "sha256_hash": sha256_text(payload),
                    "required_codes": required_codes,
                    "status": "DRAFT_BUILT",
                },
            )

        if self.submission_store is not None:
            snapshot = {key: state.get(key, {}) for key in (
                "claim", "canonical_claim", "source_context", "routing_context", "route")}
            snapshot["payload_version"] = state.get("payload_version", 0)
            self.submission_store.put("pa-draft:" + request_id, {
                "record_type": "pa_draft", "snapshot": snapshot,
                "source_hash": sha256_text(payload), "required_codes": required_codes,
            })

        return {
            **state,
            "pa_payload": payload,
            "pa_payload_uri": uri,
            "pa_payload_type": builder.content_type,
            "pa_request_id": request_id,
            "pa_display_id": display_id,
            "pa_linked_claim_id": linked_claim_id,
            "pa_payload_version": version,
            "pa_payload_validation": validation.to_dict(),
        }

    def link_to_claim(self, request_id: str, claim_id: str) -> None:
        """Call once a real claim exists for a prior auth request that was built standalone."""
        if not self.repository.get_claim_detail(claim_id):
            raise ValueError(f"Cannot link prior auth request: claim not found: {claim_id}")
        self.repository.link_prior_auth_request_to_claim(request_id=request_id, claim_id=claim_id)
