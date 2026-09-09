from __future__ import annotations
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
import re

from fastapi import FastAPI, HTTPException, Response, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
from velo_claim.submission.api import build_submission_router, reviewer
from velo_claim.submission.shafafiya import SubmissionError
from fastapi.middleware.cors import CORSMiddleware

from typing import Any

from pydantic import BaseModel, Field

from velo_claim.core.container import ServiceContainer, build_container_from_env
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.core.enums import AuditEventType
from velo_claim.core.utils import utc_now
from velo_claim.fallback.checkpoints import MemoryCheckpointStore
from velo_claim.ingestion.pdf_encounter import EncounterPdfExtractor, PdfExtractionError
from velo_claim.pipeline import run_full_pipeline

from .serializers import claim_for_api
from uuid import uuid4

logger = logging.getLogger(__name__)

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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            runtime = _services
            close = getattr(getattr(runtime, "kg_client", None), "close", None)
            if callable(close):
                close()

    app = FastAPI(
        title="Velo Claim API",
        version="0.1.0",
        description="HTTP facade for Velo Claim agents and reusable claim operations.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        expose_headers=["X-Submission-ID", "Content-Disposition"],
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
        kg = services.kg_client.diagnostics()
        return {
            "status": "ok" if kg.get("connectivity") == "healthy" else "degraded",
            "storage": type(services.repository).__name__,
            "object_store": type(services.object_store).__name__,
            "cache": type(services.cache).__name__,
            "knowledge_graph": kg,
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
            error_id = uuid4().hex
            logger.exception("Encounter pipeline failed (error_id=%s)", error_id)
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "ENCOUNTER_PIPELINE_FAILED",
                    "message": "The encounter could not be processed. Contact support with the error ID.",
                    "error_id": error_id,
                },
            ) from exc
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

    @app.post("/encounters/pdf")
    async def ingest_encounter_pdf(file: UploadFile = File(...)) -> dict[str, Any]:
        services = get_services()
        max_bytes = int(os.getenv("PDF_ENCOUNTER_MAX_BYTES", str(10 * 1024 * 1024)))
        if file.content_type not in {"application/pdf", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail={"code": "PDF_REQUIRED", "message": "Upload a PDF encounter document."})
        content = await file.read(max_bytes + 1)
        if not content or len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={"code": "PDF_SIZE_LIMIT", "message": f"PDF must be between 1 byte and {max_bytes} bytes."},
            )

        digest = hashlib.sha256(content).hexdigest()
        claim_id = f"CLM-PDF-{digest[:12].upper()}"
        existing = services.repository.get_claim_detail(claim_id)
        existing_payload = services.repository.latest_claim_payload(claim_id) if existing else None
        if existing and existing_payload:
            return {
                "status": "duplicate",
                "claim_id": claim_id,
                "claim": claim_for_api(existing, services.object_store),
                "message": "This PDF was already processed.",
            }

        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "encounter.pdf")[:120]
        source_uri = services.object_store.put_bytes(
            f"imports/encounters/{digest}/source-{safe_name}",
            content,
            content_type="application/pdf",
        )
        try:
            extraction = EncounterPdfExtractor().extract(content)
        except PdfExtractionError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc), "source_document_uri": source_uri},
            ) from exc

        extraction_uri = services.object_store.put_text(
            f"imports/encounters/{digest}/extraction.json",
            json.dumps(extraction.to_dict(), indent=2, ensure_ascii=True),
            content_type="application/json",
        )
        if not extraction.ready:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PDF_EXTRACTION_INCOMPLETE",
                    "message": "Required routing facts could not be extracted from the PDF.",
                    "missing_fields": extraction.missing_routing_fields,
                    "warnings": extraction.warnings,
                    "source_document_uri": source_uri,
                    "extraction_uri": extraction_uri,
                },
            )

        package = extraction.encounter_package
        package.setdefault("attachments", []).append(
            {
                "type": "ENCOUNTER_PDF",
                "name": safe_name,
                "content_type": "application/pdf",
                "url": source_uri,
                "status": "available",
                "sha256": digest,
            }
        )
        initial_state = {
            "claim_id": claim_id,
            "encounter_package": package,
            "jurisdiction": package.get("jurisdiction"),
            "ingestion": {
                "source": "RCM_PDF_UPLOAD",
                "source_document_uri": source_uri,
                "extraction_uri": extraction_uri,
                "sha256": digest,
                "filename": safe_name,
                "extraction_method": extraction.extraction_method,
            },
        }
        try:
            result_state = run_full_pipeline(initial_state, container=services)
        except Exception as exc:
            error_id = uuid4().hex
            logger.exception("PDF encounter pipeline failed for %s (error_id=%s)", claim_id, error_id)
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "PDF_PIPELINE_FAILED",
                    "message": "The PDF was extracted, but the claim workflow could not complete. Contact support with the error ID.",
                    "error_id": error_id,
                    "source_document_uri": source_uri,
                    "extraction_uri": extraction_uri,
                },
            ) from exc
        try:
            services.repository.insert_audit_event(
                claim_id,
                {
                    "agent": "EncounterPdfIngestion",
                    "node": "process_pdf",
                    "event_type": AuditEventType.NODE_EXIT,
                    "payload": {
                        "event_name": "PDF_ENCOUNTER_INGESTED",
                        "source_document_uri": source_uri,
                        "extraction_uri": extraction_uri,
                        "sha256": digest,
                        "warnings": extraction.warnings,
                    },
                    "ts": utc_now(),
                },
            )
        except Exception:
            logger.exception("Could not write the PDF ingestion audit event for %s", claim_id)
        detail = services.repository.get_claim_detail(claim_id)
        return {
            "status": "completed",
            "claim_id": claim_id,
            "claim": claim_for_api(detail or _state_detail(result_state), services.object_store),
            "state": _state_summary(result_state),
            "extraction": {
                "method": extraction.extraction_method,
                "page_count": extraction.page_count,
                "warnings": extraction.warnings,
                "source_document_uri": source_uri,
                "extraction_uri": extraction_uri,
            },
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
        module = PAClaimBuilderModule(repository=services.repository, object_store=services.object_store, submission_store=services.submission_store)
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



    @app.post("/prior-auth/{request_id}/link-claim")
    def link_prior_auth_to_claim(request_id: str, body: LinkPARequest, actor=Depends(reviewer)) -> dict[str, Any]:
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
        with services.submission_store.lock('target:prior_auth:' + stored_request_id):
            if any(a['kind'] == 'prior_auth' and a['entity_id'] == stored_request_id
                   for a in services.submission_store.list('delivery')):
                raise HTTPException(409, "Link the claim before exporting or sending the authorization request.")
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
    def update_status(claim_id: str, body: StatusUpdateRequest, actor=Depends(reviewer)) -> dict[str, Any]:
        services = get_services()
        if not services.repository.get_claim_detail(claim_id):
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")
        if body.status.upper() not in {"REVIEW", "NEEDS_REVIEW", "HOLD", "HOLD_CRITICAL"} or body.override:
            raise HTTPException(409, "Readiness and submission status are controlled by validation and delivery results.")
        metadata = {
            **body.metadata,
            "actor": actor,
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
                "event_type": AuditEventType.NODE_EXIT,
                "payload": {
                    "event_name": "STATUS_UPDATED",
                    "status": body.status,
                    "metadata": metadata,
                },
                "ts": utc_now(),
            },
        )
        detail = services.repository.get_claim_detail(claim_id)
        return {"ok": True, "claim": claim_for_api(detail, services.object_store)}

    @app.post("/claims/{claim_id}/actions/{action}")
    def claim_action(claim_id: str, action: str, body: ActionRequest | None = None, actor=Depends(reviewer)) -> dict[str, Any]:
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
            raise HTTPException(409, "Use payload approval and the controlled /submit endpoint.")
        elif action_key in {"hold", "hold_critical"}:
            new_status = "hold"
        else:
            new_status = str(detail.get("status") or "review")
        metadata = {**body.metadata, "actor": actor, "action": action_key, "reason": body.reason, "note": body.note}
        services.repository.update_claim_status(claim_id, new_status, metadata)
        services.repository.insert_audit_event(
            claim_id,
            {
                "agent": "VeloClaimAPI",
                "node": f"action:{action_key}",
                "event_type": AuditEventType.NODE_EXIT,
                "payload": {"event_name": "ACTION_REQUESTED", **metadata},
                "ts": utc_now(),
            },
        )
        updated = services.repository.get_claim_detail(claim_id)
        return {"ok": True, "action": action_key, "claim": claim_for_api(updated, services.object_store)}

    @app.post("/webhooks/payer/{claim_id}")
    async def payer_webhook(claim_id: str, body: dict[str, Any], actor=Depends(reviewer)) -> dict[str, Any]:
        raise HTTPException(410, "Use Shafafiya response polling or authenticated XML import.")

    @app.exception_handler(SubmissionError)
    async def submission_error(request: Request, exc: SubmissionError):
        return JSONResponse(status_code=exc.status_code, content={"error_code": exc.code, "detail": str(exc)})

    @app.post("/prior-auth/{request_id}/simulate-submit")
    @app.post("/claims/{request_id}/cancel-submission")
    def retired_simulation(request_id: str, actor=Depends(reviewer)):
        raise HTTPException(410, "Simulation and local cancellation are retired. Use the controlled submission workflow.")

    app.include_router(build_submission_router(get_services))
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
