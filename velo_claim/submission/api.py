from __future__ import annotations

import hmac
import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from velo_claim.core.env import load_env_file
from velo_claim.submission.service import SubmissionService
from velo_claim.submission.shafafiya import ShafafiyaAdapter, ShafafiyaSettings, SubmissionError


def reviewer(authorization: str | None = Header(default=None)) -> str:
    """Server-configured individual reviewer credentials; never trust actor in a body."""
    load_env_file()
    try:
        reviewers = json.loads(os.getenv('VELO_SUBMISSION_REVIEWERS', '{}'))
    except ValueError:
        raise HTTPException(503, 'Reviewer authentication is not configured.') from None
    if not isinstance(reviewers, dict) or not reviewers:
        raise HTTPException(503, 'Reviewer authentication is not configured.')
    token = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
    for configured, identity in reviewers.items():
        if isinstance(identity, str) and identity.strip() and len(configured) >= 32 and hmac.compare_digest(token, configured):
            return identity
    raise HTTPException(401, 'A valid reviewer credential is required.', headers={'WWW-Authenticate': 'Bearer'})


class ApprovalIn(BaseModel):
    payload_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    note: str = Field(min_length=1, max_length=2000)


class DeliveryIn(BaseModel):
    approval_id: str


class NoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class ImportIn(BaseModel):
    xml: str = Field(min_length=1, max_length=20 * 1024 * 1024)
    external_id: str = Field(min_length=1, max_length=200)


class ReceiptIn(NoteIn):
    transaction_id: str = Field(min_length=1, max_length=200)


def public(record):
    return {k: v for k, v in record.items() if k not in {'snapshot', 'object_uri', 'diagnostic_uri'}}


def build_submission_router(get_services):
    router = APIRouter()

    def service():
        services = get_services()
        settings = ShafafiyaSettings.from_env()
        return SubmissionService(services, settings, ShafafiyaAdapter(settings))

    @router.get('/submission/{kind}/{entity_id}/preview')
    def preview(kind: str, entity_id: str, actor=Depends(reviewer)):
        return public(service().target(kind, entity_id))

    @router.post('/submission/{kind}/{entity_id}/approve')
    def approve(kind: str, entity_id: str, body: ApprovalIn, actor=Depends(reviewer)):
        return public(service().approve(kind, entity_id, body.payload_hash, actor, body.note))

    def send(kind, entity_id, body, actor):
        svc = service()
        approval = svc.store.get('approval:' + body.approval_id)
        if not approval or approval['kind'] != kind or approval['entity_id'] != entity_id:
            raise HTTPException(409, 'Approval does not belong to this target.')
        return public(svc.deliver(body.approval_id, actor))

    @router.post('/claims/{claim_id}/submit')
    def submit_claim(claim_id: str, body: DeliveryIn, actor=Depends(reviewer)):
        return send('claim', claim_id, body, actor)

    @router.post('/prior-auth/{request_id}/submit')
    def submit_pa(request_id: str, body: DeliveryIn, actor=Depends(reviewer)):
        return send('prior_auth', request_id, body, actor)

    @router.post('/submissions/export')
    def export(body: DeliveryIn, actor=Depends(reviewer)):
        svc = service()
        attempt = svc.deliver(body.approval_id, actor, manual=True)
        payload = svc.objects.get_text(attempt['object_uri'])
        return Response(payload, media_type='application/xml', headers={
            'Content-Disposition': f'attachment; filename="{attempt["filename"]}"', 'X-Submission-ID': attempt['id']})

    @router.get('/submissions')
    def list_attempts(actor=Depends(reviewer)):
        svc = service()
        return {'deliveries': [public(a) for a in svc.store.list('delivery')
            if a['mode'] == svc.settings.mode and a['sender_id'] == svc.settings.sender_id]}

    @router.get('/submissions/responses/events')
    def list_responses(actor=Depends(reviewer)):
        svc = service()
        return {'responses': [public(a) for a in svc.store.list('response')
            if a.get('mode') == svc.settings.mode and a.get('sender_id') == svc.settings.sender_id]}

    @router.get('/submissions/{attempt_id}')
    def get_attempt(attempt_id: str, actor=Depends(reviewer)):
        return public(service().get_delivery(attempt_id))

    @router.post('/submissions/{attempt_id}/reconcile')
    def reconcile(attempt_id: str, actor=Depends(reviewer)):
        return public(service().reconcile(attempt_id))

    @router.post('/submissions/{attempt_id}/release-rejected')
    def retry_rejected(attempt_id: str, body: NoteIn, actor=Depends(reviewer)):
        return public(service().retry_rejected(attempt_id, actor, body.note))

    @router.post('/submissions/{attempt_id}/manual-receipt')
    def receipt(attempt_id: str, body: ReceiptIn, actor=Depends(reviewer)):
        svc = service()
        if svc.settings.mode != 'manual':
            raise HTTPException(409, 'Manual receipts require manual mode.')
        attempt = svc.get_delivery(attempt_id)
        with svc.store.lock('target:' + attempt['kind'] + ':' + attempt['entity_id']):
            attempt = svc.get_delivery(attempt_id)
            if attempt['delivery_status'] != 'EXPORTED':
                raise HTTPException(409, 'Only an exported payload can receive a manual receipt.')
            attempt.update(delivery_status='ACKNOWLEDGED', transaction_id=body.transaction_id,
                receipt_source='MANUAL_RECORDED', receipt_actor=actor, receipt_note=body.note)
            svc.store.put('delivery:' + attempt_id, attempt)
            svc._reflect_delivery(attempt)
            return public(attempt)

    @router.post('/submissions/responses/import')
    def import_response(body: ImportIn, actor=Depends(reviewer)):
        svc = service()
        if svc.settings.mode != 'manual':
            raise HTTPException(409, 'Manual response import requires manual mode.')
        return public(svc.import_response(body.xml, body.external_id, actor))

    @router.post('/submissions/responses/poll')
    def poll(actor=Depends(reviewer)):
        return {'results': service().poll()}

    return router
