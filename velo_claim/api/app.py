from __future__ import annotations
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from typing import Any

from pydantic import BaseModel, Field

from velo_claim.core.container import ServiceContainer, build_container_from_env
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.core.utils import utc_now
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.pipeline import run_full_pipeline

from .serializers import claim_for_api
from .webhooks import receive_payer_webhook
from uuid import uuid4

class BuildPARequest(BaseModel):
    state: dict[str, Any]
    required_codes: list[str]

class BuildClaimRequest(BaseModel):
    state: dict[str, Any]

class LinkPARequest(BaseModel):
    claim_id: str

class EncounterIngestRequest(BaseModel):
    """Raw encounter/context package.

    The API accepts a flexible dict because Velo Doctor, FHIR upload, and
    sandbox tests do not always send identical envelopes yet.
    """

    payload: dict[str, Any] | None = None


class StatusUpdateRequest(BaseModel):
    status: str
    note: str | None = None
    reason: str | None = None
    override: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(BaseModel):
    reason: str | None = None
    note: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


_services: ServiceContainer | None = None
_checkpoint_store = MemoryCheckpointStore()


def get_services() -> ServiceContainer:
    global _services
    if _services is None:
        _services = build_container_from_env()
    return _services


def create_app(services: ServiceContainer | None = None):
    # try:
    # except ImportError as exc:
    #     raise RuntimeError("Install FastAPI to run the API: pip install fastapi uvicorn") from exc

    if services is not None:
        global _services
        _services = services

    app = FastAPI(
        title="Velo Claim API",
        version="0.1.0",
        description="HTTP facade for Velo Claim agents and reusable claim operations.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        try:
            services = get_services()
        except Exception as exc:
            return {
                "status": "degraded",
                "error": str(exc),
                "timestamp": utc_now(),
            }
        return {
            "status": "ok",
            "storage": type(services.repository).__name__,
            "object_store": type(services.object_store).__name__,
            "cache": type(services.cache).__name__,
            "timestamp": utc_now(),
        }

    @app.post("/encounters")
    def ingest_encounter(body: dict[str, Any]) -> dict[str, Any]:
        services = get_services()
        initial_state = body.get("payload") if set(body.keys()) == {"payload"} else body
        if not isinstance(initial_state, dict):
            raise HTTPException(status_code=400, detail="Encounter payload must be a JSON object.")
        try:
            result_state = run_full_pipeline(initial_state, container=services)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        claim_id = (
            result_state.get("claim", {}).get("claim_id")
            or result_state.get("canonical_claim", {}).get("claim_id")
            or result_state.get("claim_id")
        )
        detail = services.repository.get_claim_detail(claim_id) if claim_id else None
        return {
            "status": "completed",
            "claim_id": claim_id,
            "claim": claim_for_api(detail or _state_detail(result_state), services.object_store),
            "state": _state_summary(result_state),
        }
    def _claim_build_response(result_state: dict[str, Any]) -> dict[str, Any]:
        claim = result_state.get("claim", {})
        return {
            "claim_id": claim.get("claim_id"),
            "version": claim.get("version"),
            "claim_format": result_state.get("claim_format"),
            "payload_status": result_state.get("payload_status"),
            "claim_payload_uri": result_state.get("claim_payload_uri"),
            "claim_payload_type": result_state.get("claim_payload_type"),
            "jurisdiction": result_state.get("jurisdiction"),
            "next_agent": result_state.get("next_agent"),
        }
    def _pa_build_response(result_state: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "claim_id": result_state.get("pa_linked_claim_id"),
            "pa_request_id": result_state.get("pa_request_id"),
            "pa_display_id": result_state.get("pa_display_id"),
            "pa_payload_uri": result_state.get("pa_payload_uri"),
            "pa_payload_type": result_state.get("pa_payload_type"),
        }

    @app.post("/prior-auth/build")
    def build_prior_auth_standalone(body: BuildPARequest) -> dict[str, Any]:
        services = get_services()
        module = PAClaimBuilderModule(repository=services.repository, object_store=services.object_store)
        try:
            result_state = module.build(body.state, body.required_codes)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _pa_build_response(result_state)

    @app.post("/claim/build")
    def build_claim(body: BuildClaimRequest) -> dict[str, Any]:
        services = get_services()
        module = ClaimBuilderModule(
            repository=services.repository,
            object_store=services.object_store,
            kg_client=services.kg_client,
            payer_rule_loader=services.payer_rule_loader,
        )
        try:
            result_state = module.build(body.state)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _claim_build_response(result_state)

    @app.get("/prior-auth/{request_id}/status")
    def get_prior_auth_status(request_id: str) -> dict[str, Any]:
        services = get_services()
        request = services.repository.get_prior_auth_request(request_id)
        if not request:
            raise HTTPException(status_code=404, detail=f"Prior auth request not found: {request_id}")

        stored_request_id = str(request.get("id") or request.get("request_id") or request_id)
        response = services.repository.get_latest_prior_auth_response(stored_request_id)
        return {
            "request_id": request_id,
            "display_id": request.get("display_id"),
            "claim_id": request.get("claim_id"),
            "standard": request.get("standard"),
            "status": request.get("status"),
            "submitted_at": request.get("submitted_at"),
            "decided": response is not None,
            "response": {
                "status": response.get("status"),
                "pre_auth_ref": response.get("pre_auth_ref"),
                "received_at": response.get("received_at"),
                "payer_response": response.get("payer_response"),
            } if response else None,
        }
    @app.post("/prior-auth/{request_id}/simulate-submit")
    def simulate_submit_prior_auth(request_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        DEV/TEST ONLY — simulates submitting to a payer and receiving an
        immediate response. No real payer integration exists; this exists
        purely to exercise the full PA lifecycle locally.
        """
        services = get_services()
        request = services.repository.get_prior_auth_request(request_id)
        if not request:
            raise HTTPException(status_code=404, detail=f"Prior auth request not found: {request_id}")
        stored_request_id = str(request.get("id") or request.get("request_id") or request_id)

        # mark as submitted
        services.repository.update_prior_auth_submitted(stored_request_id)

        # simulate a payer decision
        import random
        requested_decision = str((body or {}).get("decision") or "").strip().lower()
        if requested_decision not in {"", "approved", "denied"}:
            raise HTTPException(status_code=400, detail="decision must be approved or denied")
        approved = requested_decision == "approved" if requested_decision else random.random() < 0.85
        fake_response = {
            "decision": "approved" if approved else "denied",
            "pre_auth_ref": f"AUTH-{uuid4().hex[:8].upper()}" if approved else None,
            "message": "Approved by payer" if approved else "Denied: coverage exclusion",
        }

        services.repository.insert_prior_auth_response(
            stored_request_id,
            {
                "status": "APPROVED" if approved else "DENIED_NEEDS_REVIEW",
                "pre_auth_ref": fake_response["pre_auth_ref"],
                "payer_response": fake_response,
                "received_via": "MANUAL",
            },
        )

        return {"ok": True, "request_id": stored_request_id, "simulated_response": fake_response}

    @app.post("/claims/{claim_id}/cancel-submission")
    def simulate_cancel_submission(claim_id: str) -> dict[str, Any]:
        """
        DEV/TEST ONLY — simulates cancelling the most recent submission for a
        claim, so it can be rebuilt and resubmitted with updated content.
        """
        services = get_services()
        detail = services.repository.get_claim_detail(claim_id)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")

        cancelled = services.repository.cancel_latest_submission(claim_id)
        services.repository.update_claim_status(claim_id, "DRAFT_BUILDING", {"updated_via": "cancel-submission"})

        return {"ok": True, "claim_id": claim_id, "cancelled": cancelled}

    @app.post("/claims/{claim_id}/submit")
    def simulate_submit_claim(claim_id: str) -> dict[str, Any]:
        """
        DEV/TEST ONLY — simulates submitting a built claim payload to a payer
        and receiving an immediate response. No real payer integration exists;
        this exists purely to exercise the full claim lifecycle locally.
        """
        services = get_services()
        detail = services.repository.get_claim_detail(claim_id)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")

        payload_row = detail.get("claim_payload") or {}
        object_uri = payload_row.get("object_uri") or detail.get("claim_payload_uri")
        if not object_uri:
            raise HTTPException(status_code=400, detail=f"No built claim payload found for {claim_id}. Build the claim first.")

        payer_id = detail.get("payer_id")
        fingerprint = payload_row.get("sha256_hash") or detail.get("claim_payload_hash")

        submission_id = services.repository.insert_submission_attempt(
            claim_id,
            {
                "channel": "SIMULATED",
                "object_uri": object_uri,
            },
        )

        import random
        approved = random.random() < 0.85
        fake_response = {
            "decision": "accepted" if approved else "submitted",
            "message": "Accepted by payer" if approved else "Submitted — no decision yet",
        }
        new_status = "ACCEPTED" if approved else "SUBMITTED"

        services.repository.update_submission_response(
            submission_id,
            {"response_status": new_status, "payer_response": fake_response},
        )
        services.repository.update_claim_status(claim_id, new_status, {"updated_via": "simulate-submit"})

        return {
            "ok": True,
            "claim_id": claim_id,
            "submission_id": submission_id,
            "status": new_status,
            "simulated_response": fake_response,
        }

    @app.post("/prior-auth/{request_id}/link-claim")
    def link_prior_auth_to_claim(request_id: str, body: LinkPARequest) -> dict[str, Any]:
        services = get_services()

        request = services.repository.get_prior_auth_request(request_id)
        if not request:
            raise HTTPException(status_code=404, detail=f"Prior auth request not found: {request_id}")

        if request.get("claim_id"):
            raise HTTPException(
                status_code=409,
                detail=f"Prior auth request {request_id} is already linked to claim {request['claim_id']}",
            )

        if not services.repository.get_claim_detail(body.claim_id):
            raise HTTPException(status_code=404, detail=f"Claim not found: {body.claim_id}")

        stored_request_id = str(request.get("id") or request.get("request_id") or request_id)
        services.repository.link_prior_auth_request_to_claim(
            request_id=stored_request_id,
            claim_id=body.claim_id,
        )

        updated = services.repository.get_prior_auth_request(request_id)
        return {"ok": True, "request_id": request_id, "claim_id": updated.get("claim_id")}

    @app.get("/claims")
    def list_claims(limit: int = 100) -> dict[str, Any]:
        services = get_services()
        rows = services.repository.list_claim_summaries(limit=limit)
        details = [
            services.repository.get_claim_detail(row.get("claim_id")) or row
            for row in rows
            if row.get("claim_id")
        ]
        claims = [claim_for_api(detail, services.object_store) for detail in details]
        return {"claims": claims, "count": len(claims)}

    @app.get("/claims/{claim_id}")
    def get_claim(claim_id: str) -> dict[str, Any]:
        services = get_services()
        detail = services.repository.get_claim_detail(claim_id)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")
        return claim_for_api(detail, services.object_store)

    @app.get("/claims/{claim_id}/payload")
    def get_claim_payload(claim_id: str) -> Response:
        services = get_services()
        detail = services.repository.get_claim_detail(claim_id)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")
        payload_row = detail.get("claim_payload") or {}
        object_uri = payload_row.get("object_uri") or detail.get("claim_payload_uri")
        if not object_uri:
            raise HTTPException(status_code=404, detail=f"No built claim payload is stored for {claim_id}.")
        try:
            payload = services.object_store.get_text(object_uri)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Stored payload could not be read: {exc}") from exc
        media_type = str(payload_row.get("payload_type") or "text/plain")
        if media_type == "xml":
            media_type = "application/xml"
        return Response(content=payload, media_type=media_type)

    @app.patch("/claims/{claim_id}/status")
    def update_status(claim_id: str, body: StatusUpdateRequest) -> dict[str, Any]:
        services = get_services()
        if not services.repository.get_claim_detail(claim_id):
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")
        metadata = {
            **body.metadata,
            "note": body.note,
            "reason": body.reason,
            "override": body.override,
            "updated_via": "api",
        }
        services.repository.update_claim_status(claim_id, body.status, metadata)
        services.repository.insert_audit_event(
            claim_id,
            {
                "agent": "VeloClaimAPI",
                "node": "update_status",
                "event_type": "STATUS_UPDATED",
                "payload": {"status": body.status, "metadata": metadata},
                "ts": utc_now(),
            },
        )
        detail = services.repository.get_claim_detail(claim_id)
        return {"ok": True, "claim": claim_for_api(detail, services.object_store)}

    @app.post("/claims/{claim_id}/actions/{action}")
    def claim_action(claim_id: str, action: str, body: ActionRequest | None = None) -> dict[str, Any]:
        services = get_services()
        detail = services.repository.get_claim_detail(claim_id)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")
        body = body or ActionRequest()
        action_key = action.strip().lower()
        if action_key in {"send_back", "sendback"}:
            new_status = "review"
        elif action_key in {"escalate", "needs_review"}:
            new_status = "review"
        elif action_key in {"approve_submit", "submit", "submitted"}:
            new_status = "submitted"
        elif action_key in {"hold", "hold_critical"}:
            new_status = "hold"
        else:
            new_status = str(detail.get("status") or "review")
        metadata = {**body.metadata, "action": action_key, "reason": body.reason, "note": body.note}
        services.repository.update_claim_status(claim_id, new_status, metadata)
        services.repository.insert_audit_event(
            claim_id,
            {
                "agent": "VeloClaimAPI",
                "node": f"action:{action_key}",
                "event_type": "ACTION_REQUESTED",
                "payload": metadata,
                "ts": utc_now(),
            },
        )
        updated = services.repository.get_claim_detail(claim_id)
        return {"ok": True, "action": action_key, "claim": claim_for_api(updated, services.object_store)}

    @app.post("/webhooks/payer/{claim_id}")
    async def payer_webhook(claim_id: str, body: dict[str, Any]) -> dict[str, Any]:
        services = get_services()
        return receive_payer_webhook(
            claim_id=claim_id,
            body=body,
            repository=services.repository,
            cache=services.cache,
            checkpoint_store=_checkpoint_store,
            object_store=services.object_store,
        )

    return app


def _state_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim": state.get("claim"),
        "claim_format": str(state.get("claim_format")),
        "jurisdiction": str(state.get("jurisdiction")),
        "payload_status": str(state.get("payload_status")),
        "final_status": str(state.get("final_status")),
        "score": state.get("score"),
        "claim_payload_uri": state.get("claim_payload_uri"),
        "claim_payload_type": state.get("claim_payload_type"),
        "validation_report_uri": state.get("validation_report_uri"),
        "errors": state.get("errors", []),
        "warnings": state.get("warnings", []),
    }


def _state_detail(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": state.get("claim", {}).get("claim_id") or state.get("canonical_claim", {}).get("claim_id"),
        "status": state.get("payload_status"),
        "route": state.get("route", {}),
        "canonical_claim": state.get("canonical_claim", {}),
        "source_context": state.get("source_context", {}),
        "claim_payload": {
            "version": state.get("payload_version", 1),
            "payload_type": state.get("claim_payload_type"),
            "object_uri": state.get("claim_payload_uri"),
            "sha256_hash": "",
            "status": state.get("payload_status"),
        },
        "validation_report": state.get("validation_report", {}),
        "audit_events": [],
    }


app = create_app()
