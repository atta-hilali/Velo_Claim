from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from lxml import etree

from velo_claim.core.utils import utc_now
from velo_claim.submission.shafafiya import MAX_BYTES, SubmissionError, TransportUnknown, parse_xml

SCHEMAS = Path(__file__).resolve().parents[2] / 'data' / 'schemas' / 'shafafiya' / 'v2.0'
ACTIVE_DELIVERIES = {'SUBMITTING', 'DELIVERY_UNKNOWN', 'ACKNOWLEDGED', 'EXPORTED', 'RESPONSE_RECEIVED'}


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def text_at(root, path):
    return (root.findtext(path) or '').strip()


def schema_check(root, name):
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        schema = etree.XMLSchema(etree.parse(str(SCHEMAS / name), parser))
        if not schema.validate(root):
            raise SubmissionError('SCHEMA_INVALID', 'Payload does not conform to the Shafafiya XSD: ' +
                                  '; '.join(f'{e.type_name} at line {e.line}' for e in schema.error_log), 422)
    except (OSError, etree.XMLSchemaParseError):
        raise SubmissionError('SCHEMA_UNAVAILABLE', 'The required Shafafiya XSD is unavailable.', 503) from None


def copy_context(claim):
    payer = {k: v for k, v in claim.get('payer', {}).items() if k not in {'eligibility_status', 'eligibility_ref', 'benefit_summary'}}
    return {**claim, 'payer': payer}


class SubmissionService:
    def __init__(self, services, settings, adapter=None):
        self.services = services
        self.repo = services.repository
        self.objects = services.object_store
        self.store = services.submission_store
        self.settings = settings
        self.adapter = adapter

    def _enabled(self):
        if self.settings.mode == 'disabled':
            raise SubmissionError('DISABLED', 'Submission is disabled; configure manual or PTE mode.', 503)
        if self.settings.mode == 'production':
            # Live clinical/rule validation is not wired in this project yet.
            raise SubmissionError('PRODUCTION_NOT_READY', 'Production submission is blocked until live knowledge graph and payer-rule validation are integrated and verified.', 503)
        if not self.settings.sender_id:
            raise SubmissionError('CONFIGURATION', 'Configure SHAFAFIYA_SENDER_ID.', 503)

    def _network(self):
        if self.settings.mode not in {'pte', 'production'} or self.adapter is None:
            raise SubmissionError('NETWORK_DISABLED', 'Use the manual export/import workflow in this mode.', 503)
        if not self.store.durable:
            raise SubmissionError('DURABLE_STORAGE_REQUIRED', 'Network submission requires PostgreSQL-backed tracking.', 503)
        self._enabled()
        self.adapter.connect()

    def target(self, kind, entity_id):
        self._enabled()
        if kind == 'claim':
            detail = self.repo.get_claim_detail(entity_id)
            if not detail:
                raise SubmissionError('NOT_FOUND', 'Claim not found.', 404)
            row = detail.get('claim_payload') or {}
            if detail.get('route', {}).get('claim_standard') != 'SHAFAFIYA':
                raise SubmissionError('UNSUPPORTED_ROUTE', 'This submission service supports Shafafiya only.', 422)
            report = detail.get('validation_report') or {}
            report_row = detail.get('validation_report_row') or {}
            if str(detail.get('status')).upper() not in {'READY_TO_SUBMIT', 'READY'}:
                raise SubmissionError('NOT_READY', 'The current claim is not ready for submission.')
            if report.get('status') != 'READY_TO_SUBMIT' or int(report_row.get('version', 0)) != int(row.get('version', -1)):
                raise SubmissionError('VALIDATION_REQUIRED', 'The current payload version needs a successful validation report.')
            if any(str(i.get('severity')).upper() in {'CRITICAL', 'ERROR'} for i in report.get('issues', [])):
                raise SubmissionError('BLOCKING_ISSUES', 'Resolve all validation errors before approval.')
            source_payload = self.objects.get_text(row['object_uri'])
            source_hash = digest(source_payload)
            if report.get('payload_hash') != source_hash or row.get('sha256_hash') != source_hash:
                raise SubmissionError('STALE_VALIDATION', 'Revalidate the exact current payload before approving it.')
            snapshot = {'claim': {'claim_id': entity_id}, 'payload_version': row['version'],
                'canonical_claim': detail.get('canonical_claim', {}), 'route': detail['route'],
                'source_context': detail.get('source_context', {})}
            version = row['version']
            claim_id = entity_id
        elif kind == 'prior_auth':
            row = self.repo.get_prior_auth_request(entity_id)
            if not row:
                raise SubmissionError('NOT_FOUND', 'Prior authorization request not found.', 404)
            entity_id = str(row.get('id') or row.get('request_id'))
            if str(row.get('standard')) != 'SHAFAFIYA':
                raise SubmissionError('UNSUPPORTED_ROUTE', 'This submission service supports Shafafiya only.', 422)
            source_payload = self.objects.get_text(row['object_uri'])
            source_hash = digest(source_payload)
            draft = self.store.get('pa-draft:' + entity_id)
            if not draft or draft['source_hash'] != source_hash:
                raise SubmissionError('PA_REBUILD_REQUIRED', 'Rebuild this prior authorization request to capture its continuation context.')
            snapshot = draft['snapshot']
            version = 1
            claim_id = row.get('claim_id')
        else:
            raise SubmissionError('INVALID_KIND', 'Use claim or prior_auth.', 422)
        root = parse_xml(source_payload)
        if text_at(root, 'Header/SenderID') != self.settings.sender_id:
            raise SubmissionError('SENDER_MISMATCH', 'Payload sender does not match the configured facility.', 422)
        payer = text_at(root, 'Header/ReceiverID')
        if not payer:
            raise SubmissionError('PAYER_MISSING', 'Payload receiver is missing.', 422)
        node = root.find('Claim' if kind == 'claim' else 'Authorization')
        if node is None or not text_at(node, 'ID'):
            raise SubmissionError('IDENTITY_MISSING', 'Payload identity is missing.', 422)
        # The original builders use VALIDATE_ONLY. Transform BEFORE approval,
        # show the exact bytes to the reviewer, then never mutate them on send.
        flag = root.find('Header/DispositionFlag')
        if flag is None:
            raise SubmissionError('DISPOSITION_MISSING', 'Payload disposition flag is missing.', 422)
        flag.text = 'PRODUCTION' if self.settings.mode == 'production' else 'PTE_SUBMIT'
        schema_check(root, 'ClaimSubmission.xsd' if kind == 'claim' else 'PriorRequest.xsd')
        payload = etree.tostring(root, encoding='utf-8', xml_declaration=True).decode()
        return {'kind': kind, 'entity_id': entity_id, 'claim_id': claim_id, 'version': version,
            'source_hash': source_hash, 'payload_hash': digest(payload), 'payload': payload,
            'payer_id': payer, 'sender_id': self.settings.sender_id, 'wire_id': text_at(node, 'ID'),
            'mode': self.settings.mode, 'snapshot': snapshot}

    def approve(self, kind, entity_id, expected_hash, actor, note):
        with self.store.lock('target:' + kind + ':' + entity_id):
            target = self.target(kind, entity_id)
            if not actor or not note.strip():
                raise SubmissionError('APPROVAL_REQUIRED', 'A named reviewer and approval note are required.', 422)
            if target['payload_hash'] != expected_hash:
                raise SubmissionError('STALE_PAYLOAD', 'Payload changed. Preview it again before approval.')
            approval_id = str(uuid4())
            payload = target.pop('payload')
            uri = self.objects.put_text(f'submissions/approvals/{approval_id}/payload.xml', payload, content_type='application/xml')
            approval = {**target, 'id': approval_id, 'record_type': 'approval', 'actor': actor,
                'note': note, 'approved_at': utc_now(), 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                'object_uri': uri}
            self.store.put('approval:' + approval_id, approval)
            return approval

    def _approved(self, approval_id):
        approval = self.store.get('approval:' + approval_id)
        if not approval:
            raise SubmissionError('APPROVAL_REQUIRED', 'Approval record not found.', 404)
        if approval['mode'] != self.settings.mode or approval['sender_id'] != self.settings.sender_id:
            raise SubmissionError('ENVIRONMENT_CHANGED', 'Obtain approval for the current submission environment.')
        if datetime.fromisoformat(approval['expires_at']) < datetime.now(timezone.utc):
            raise SubmissionError('APPROVAL_EXPIRED', 'Approval expired; request a fresh approval.')
        target = self.target(approval['kind'], approval['entity_id'])
        if any(target[k] != approval[k] for k in ('version', 'source_hash', 'payload_hash', 'payer_id', 'wire_id', 'claim_id')):
            raise SubmissionError('STALE_APPROVAL', 'Payload changed after approval.')
        payload = self.objects.get_text(approval['object_uri'])
        if digest(payload) != approval['payload_hash']:
            raise SubmissionError('PAYLOAD_TAMPERED', 'Approved payload integrity check failed.')
        return approval, payload

    def deliver(self, approval_id, actor, manual=False):
        self._enabled()
        approval = self.store.get('approval:' + approval_id)
        if not approval:
            raise SubmissionError('APPROVAL_REQUIRED', 'Approval record not found.', 404)
        if approval['mode'] != self.settings.mode or approval['sender_id'] != self.settings.sender_id:
            raise SubmissionError('ENVIRONMENT_CHANGED', 'Obtain approval for the current submission environment.')
        with self.store.lock('target:' + approval['kind'] + ':' + approval['entity_id']):
            # Repeated requests return the existing attempt even if the claim
            # has moved out of READY_TO_SUBMIT in the meantime.
            attempts = [a for a in self.store.list('delivery') if a['mode'] == self.settings.mode and
                        a['kind'] == approval['kind'] and a['entity_id'] == approval['entity_id']]
            for attempt in attempts:
                if attempt['payload_hash'] == approval['payload_hash'] and attempt['delivery_status'] != 'RETRY_RELEASED':
                    self._reflect_delivery(attempt)
                    return attempt
                if attempt['delivery_status'] in ACTIVE_DELIVERIES:
                    raise SubmissionError('EXISTING_DELIVERY', 'An earlier version has an active or uncertain delivery. Reconcile it first.')
            approval, payload = self._approved(approval_id)
            if manual:
                if self.settings.mode != 'manual':
                    raise SubmissionError('MODE_MISMATCH', 'Manual export requires manual mode.')
            else:
                self._network()  # WSDL/config failures occur before recording an attempted upload.
            attempt_id = str(uuid4())
            attempt = {**approval, 'id': attempt_id, 'record_type': 'delivery', 'approval_id': approval_id,
                'delivery_status': 'EXPORTED' if manual else 'SUBMITTING', 'payer_decision': 'PENDING',
                'filename': 'vc-' + attempt_id + '.xml', 'submitted_by': actor, 'created_at': utc_now(),
                'transaction_id': None}
            self.store.put('delivery:' + attempt_id, attempt)  # Commit before network I/O.
            if manual:
                return attempt
            try:
                reply = self.adapter.upload(payload.encode(), attempt['filename'])
            except TransportUnknown:
                attempt['delivery_status'] = 'DELIVERY_UNKNOWN'
                attempt['error_code'] = 'DELIVERY_UNKNOWN'
            else:
                code = reply['code']
                attempt['platform_code'] = code
                # Restricted object artifact retains diagnostic detail; public
                # status responses expose codes, not payer/PHI-bearing reports.
                diagnostic = {k: (base64.b64encode(v).decode() if isinstance(v, bytes) else v) for k, v in reply.items()}
                attempt['diagnostic_uri'] = self.objects.put_text(f'submissions/{attempt_id}/upload-result.json', json.dumps(diagnostic), content_type='application/json')
                if code in {0, 1} and reply.get('transaction_id'):
                    attempt['delivery_status'] = 'ACKNOWLEDGED'
                    attempt['transaction_id'] = str(reply['transaction_id'])
                elif code in {-1, -2, -3, -7, -12}:
                    attempt['delivery_status'] = 'REJECTED'
                    attempt['error_code'] = 'AUTHENTICATION_FAILED' if code == -1 else 'UPLOAD_REJECTED'
                else:
                    attempt['delivery_status'] = 'DELIVERY_UNKNOWN'
                    attempt['error_code'] = 'DELIVERY_UNKNOWN'
            self.store.put('delivery:' + attempt_id, attempt)
            self._reflect_delivery(attempt)
            return attempt

    def _reflect_delivery(self, attempt):
        if attempt['delivery_status'] == 'ACKNOWLEDGED' and not attempt.get('reflected_at'):
            if attempt['kind'] == 'claim':
                self.repo.update_claim_status(attempt['entity_id'], 'SUBMITTED', {'delivery_id': attempt['id'], 'payer_decision': 'PENDING'})
            else:
                self.repo.update_prior_auth_submitted(attempt['entity_id'])
            attempt['reflected_at'] = utc_now()
            self.store.put('delivery:' + attempt['id'], attempt)

    def reconcile(self, attempt_id):
        self._network()
        attempt = self.get_delivery(attempt_id)
        with self.store.lock('target:' + attempt['kind'] + ':' + attempt['entity_id']):
            attempt = self.get_delivery(attempt_id)
            if attempt['delivery_status'] not in {'SUBMITTING', 'DELIVERY_UNKNOWN'}:
                return attempt
            matches = self.adapter.search_sent(attempt['filename'], attempt['payer_id'], attempt['kind'] == 'prior_auth')
            exact = {r.get('FileID'): r for r in matches if r.get('FileName') == attempt['filename'] and
                     r.get('SenderID') == attempt['sender_id'] and r.get('ReceiverID') == attempt['payer_id'] and r.get('FileID')}
            if len(exact) == 1:
                file_id = next(iter(exact))
                _, content = self.adapter.download(file_id)
                if digest(content) == attempt['payload_hash']:
                    attempt.update(delivery_status='ACKNOWLEDGED', transaction_id=file_id, reconciled_at=utc_now())
            attempt['last_reconciliation_at'] = utc_now()
            # No match is NOT proof of non-delivery (delays/search limits).
            self.store.put('delivery:' + attempt_id, attempt)
            self._reflect_delivery(attempt)
            return attempt

    def get_delivery(self, attempt_id):
        attempt = self.store.get('delivery:' + attempt_id)
        if not attempt or attempt['mode'] != self.settings.mode or attempt['sender_id'] != self.settings.sender_id:
            raise SubmissionError('NOT_FOUND', 'Delivery not found in this environment.', 404)
        return attempt

    def poll(self):
        self._network()
        files = self.adapter.pending() + self.adapter.pending(prior_auth=True)
        results = []
        for file_id, entry in {r['FileID']: r for r in files if r.get('FileID')}.items():
            if entry.get('ReceiverID') != self.settings.sender_id:
                continue
            try:
                _, content = self.adapter.download(file_id)
                event = self.import_response(content, file_id, 'shafafiya-poll', expected_sender=entry.get('SenderID'))
                if event['status'] == 'PROCESSED':
                    self.adapter.acknowledge(file_id)  # Only after all records have been persisted/applied.
                results.append({'file_id': file_id, 'status': event['status']})
            except SubmissionError as exc:
                results.append({'file_id': file_id, 'status': 'RETRY_OR_REVIEW', 'error_code': exc.code})
        return results

    def import_response(self, content, external_id, actor, expected_sender=None):
        self._enabled()
        if isinstance(content, str):
            content = content.encode()
        if len(content) > MAX_BYTES:
            raise SubmissionError('FILE_TOO_LARGE', 'Response file exceeds the supported size.', 413)
        event_id = digest(self.settings.mode + ':' + self.settings.sender_id + ':' + digest(content))
        with self.store.lock('response:' + event_id):
            event = self.store.get('response:' + event_id)
            if event and event['status'] == 'PROCESSED':
                return event
            if not event:
                uri = self.objects.put_text(f'submissions/responses/{event_id}.base64', base64.b64encode(content).decode(), content_type='text/plain')
                event = {'id': event_id, 'record_type': 'response', 'status': 'RECEIVED', 'external_id': external_id,
                         'actor': actor, 'object_uri': uri, 'received_at': utc_now(), 'completed': [],
                         'mode': self.settings.mode, 'sender_id': self.settings.sender_id}
                self.store.put('response:' + event_id, event)
            try:
                records = self._parse_responses(content, expected_sender)
                for index, (attempt, decision) in enumerate(records):
                    key = f'{event_id}:{index}'
                    if key in event['completed']:
                        continue
                    with self.store.lock('target:' + attempt['kind'] + ':' + attempt['entity_id']):
                        self._apply_response(attempt, decision, key)
                    event['completed'].append(key)
                    self.store.put('response:' + event_id, event)
                event['status'] = 'PROCESSED'
                event.pop('error_code', None)
            except SubmissionError as exc:
                event.update(status='NEEDS_REVIEW', error_code=exc.code)
            except Exception:
                event.update(status='RETRY_OR_REVIEW', error_code='RESPONSE_PROCESSING_FAILED')
            self.store.put('response:' + event_id, event)
            return event

    def _parse_responses(self, content, expected_sender):
        documents = [content]
        if zipfile.is_zipfile(io.BytesIO(content)):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    entries = [i for i in archive.infolist() if not i.is_dir()]
                    if not entries or len(entries) > 100 or sum(i.file_size for i in entries) > MAX_BYTES:
                        raise ValueError('Archive size')
                    documents = [archive.read(i) for i in entries]
            except (ValueError, zipfile.BadZipFile, RuntimeError):
                raise SubmissionError('INVALID_ARCHIVE', 'Response archive cannot be processed.', 422) from None
        records = []
        attempts = self.store.list('delivery')
        for document in documents:
            root = parse_xml(document)
            root_name = etree.QName(root).localname
            if root_name not in {'Prior.Authorization', 'Remittance.Advice'}:
                raise SubmissionError('UNSUPPORTED_RESPONSE', 'Expected Prior.Authorization or Remittance.Advice.', 422)
            payer = text_at(root, 'Header/SenderID')
            receiver = text_at(root, 'Header/ReceiverID')
            if receiver != self.settings.sender_id or not payer or (expected_sender and expected_sender != payer):
                raise SubmissionError('RESPONSE_PARTY_MISMATCH', 'Response parties do not match this mailbox.', 422)
            if root_name == 'Prior.Authorization':
                schema_check(root, 'PriorAuthorization.xsd')
            else:
                schema_check(root, 'RemittanceAdvice.xsd')
            kind, tag = ('prior_auth', 'Authorization') if root_name == 'Prior.Authorization' else ('claim', 'Claim')
            nodes = root.findall(tag)
            if not nodes:
                raise SubmissionError('EMPTY_RESPONSE', 'No transaction records found.', 422)
            for node in nodes:
                matches = [a for a in attempts if a['kind'] == kind and a['wire_id'] == text_at(node, 'ID') and
                    a['payer_id'] == payer and a['sender_id'] == receiver and a['mode'] == self.settings.mode and
                    a['delivery_status'] in ACTIVE_DELIVERIES]
                if len(matches) != 1:
                    raise SubmissionError('AMBIGUOUS_RESPONSE', 'Response does not uniquely match a submitted request.', 422)
                attempt = matches[0]
                decision = self._pa_decision(node, attempt) if kind == 'prior_auth' else {
                    'status': 'ADJUDICATED_NEEDS_REVIEW', 'payer_claim_id': text_at(node, 'IDPayer'),
                    'denial_code': text_at(node, 'DenialCode'),
                    'activities': [dict((etree.QName(c).localname, c.text) for c in a if len(c) == 0) for a in node.findall('Activity')]}
                records.append((attempt, decision))
        return records

    def _pa_decision(self, node, attempt):
        request_root = parse_xml(self.objects.get_text(attempt['object_uri']))
        requested = {text_at(a, 'ID'): a for a in request_root.findall('Authorization/Activity')}
        received = node.findall('Activity')
        codes = []
        quantities = {}
        start = None
        end = None
        try:
            start = datetime.strptime(text_at(node, 'Start'), '%d/%m/%Y %H:%M')
            end = datetime.strptime(text_at(node, 'End'), '%d/%m/%Y %H:%M')
            valid_dates = start <= end
        except ValueError:
            valid_dates = False
        fully_approved = bool(requested) and valid_dates and bool(text_at(node, 'IDPayer')) and not text_at(node, 'DenialCode')
        fully_approved = fully_approved and len(received) == len(requested) and len({text_at(a, 'ID') for a in received}) == len(received)
        denied = bool(text_at(node, 'DenialCode'))
        for activity in received:
            original = requested.get(text_at(activity, 'ID'))
            if original is None or any(text_at(activity, k) != text_at(original, k) for k in ('Code', 'Type')):
                raise SubmissionError('ACTIVITY_MISMATCH', 'PA response contains an unexpected activity.', 422)
            try:
                quantity = Decimal(text_at(activity, 'Quantity'))
                payment = Decimal(text_at(activity, 'PaymentAmount'))
                required_quantity = Decimal(text_at(original, 'Quantity'))
                required_net = Decimal(text_at(original, 'Net'))
                covered = all(x.is_finite() for x in (quantity, payment, required_quantity, required_net)) and quantity >= required_quantity > 0 and payment >= required_net and payment > 0
            except InvalidOperation:
                covered = False
            denial = text_at(activity, 'DenialCode')
            denied = denied or bool(denial)
            fully_approved = fully_approved and covered and not denial
            codes.append(text_at(activity, 'Code'))
            quantities[text_at(activity, 'Code')] = text_at(activity, 'Quantity')
        status = 'approved' if fully_approved else ('denied' if denied else 'partial_or_unknown')
        return {'status': status, 'pre_auth_ref': text_at(node, 'IDPayer'), 'payer_id': attempt['payer_id'],
            'cpt_codes': codes, 'approved_quantities': quantities,
            'valid_from': start.isoformat() if start is not None and valid_dates else None,
            'valid_to': end.isoformat() if end is not None and valid_dates else None,
            'claim_id': attempt.get('claim_id'), 'request_id': attempt['entity_id']}

    def _apply_response(self, attempt, decision, event_key):
        applied = self.store.get('applied:' + event_key)
        if applied and applied.get('status') == 'DONE':
            return
        attempt = self.get_delivery(attempt['id'])
        previous = attempt.get('decision_event')
        if previous and previous != event_key:
            raise SubmissionError('ADDITIONAL_DECISION', 'A further payer decision requires manual reconciliation.')
        attempt.update(delivery_status='RESPONSE_RECEIVED', payer_decision=decision['status'],
                       decision=decision, decision_event=event_key)
        self.store.put('delivery:' + attempt['id'], attempt)
        if attempt['kind'] == 'prior_auth':
            latest = self.repo.get_latest_prior_auth_response(attempt['entity_id']) or {}
            if latest.get('response_event') != event_key and (latest.get('payer_response') or {}).get('response_event') != event_key:
                self.repo.insert_prior_auth_response(attempt['entity_id'], {**decision, 'response_event': event_key})
            if decision['status'] == 'approved' and attempt.get('claim_id'):
                try:
                    self._continue_claim(attempt, decision, event_key)
                except Exception:
                    self.repo.update_claim_status(attempt['claim_id'], 'NEEDS_REVIEW', {'pa_response_event': event_key, 'continuation': 'FAILED'})
                    raise
            elif attempt.get('claim_id'):
                self.repo.update_claim_status(attempt['claim_id'], 'NEEDS_REVIEW', {'pa_response_event': event_key})
        elif attempt.get('claim_id'):
            # A remittance is not a blanket approval or proof of settled cash.
            self.repo.update_claim_status(attempt['claim_id'], 'NEEDS_REVIEW', {'payer_response_event': event_key})
        self.store.put('applied:' + event_key, {'status': 'DONE', 'at': utc_now()})

    def _continue_claim(self, attempt, decision, event_key):
        from velo_claim.agents.claim_validation import run_claim_validation
        from velo_claim.builders.claim.builder import ClaimBuilderModule
        from velo_claim.checks.prior_auth import auth_valid
        detail = self.repo.get_claim_detail(attempt['claim_id'])
        snapshot = attempt['snapshot']
        claim = detail.get('canonical_claim', {}) if detail else {}
        expected = snapshot.get('canonical_claim', {})
        # Link only the source encounter covered by the approved request.
        comparison_claim = copy_context(claim)
        comparison_expected = copy_context(expected)
        if not detail or any(comparison_claim.get(k) != comparison_expected.get(k) for k in ('patient', 'payer', 'provider', 'procedures', 'line_items', 'encounter')):
            raise SubmissionError('CLAIM_CHANGED', 'Claim context changed; review the authorization before rebuilding.')
        if not all(auth_valid(decision, code, claim.get('encounter', {}).get('service_date')) for code in decision['cpt_codes']):
            raise SubmissionError('PA_DATE_MISMATCH', 'Authorization does not cover the service date.')
        for delivery in self.store.list('delivery'):
            if delivery['kind'] == 'claim' and delivery['entity_id'] == attempt['claim_id'] and delivery['delivery_status'] in ACTIVE_DELIVERIES:
                raise SubmissionError('CLAIM_ALREADY_SENT', 'Reconcile the existing claim submission before rebuilding.')
        state = {**snapshot, 'canonical_claim': {**claim, 'pre_auth_ref': decision['pre_auth_ref']},
                 'claim': {'claim_id': attempt['claim_id']}, 'payload_version': detail['claim_payload']['version'],
                 'payload_status': 'DRAFT_BUILT', 'errors': [], 'warnings': []}
        # A retry after a crash must not create another version if the rebuilt
        # payload already contains this authorization.
        payload = self.objects.get_text(detail['claim_payload']['object_uri'])
        if claim.get('pre_auth_ref') != decision['pre_auth_ref'] or decision['pre_auth_ref'] not in payload:
            state = ClaimBuilderModule(repository=self.repo, object_store=self.objects,
                kg_client=self.services.kg_client, payer_rule_loader=self.services.payer_rule_loader).build(state)
        else:
            state.update(claim_payload=payload, claim_payload_uri=detail['claim_payload']['object_uri'], claim_payload_type='application/xml')
        run_claim_validation(state, container=self.services)
        # The new payload always needs its own human approval; never send here.

    def retry_rejected(self, attempt_id, actor, note):
        """Only an explicit definitive rejection may be retried; preserve history."""
        attempt = self.get_delivery(attempt_id)
        with self.store.lock('target:' + attempt['kind'] + ':' + attempt['entity_id']):
            attempt = self.get_delivery(attempt_id)
            if attempt['delivery_status'] != 'REJECTED' or not note.strip():
                raise SubmissionError('RETRY_NOT_ALLOWED', 'Only a definitively rejected upload can be released for a fresh approved attempt.')
            attempt.update(delivery_status='RETRY_RELEASED', retry_released_by=actor, retry_reason=note, retry_released_at=utc_now())
            self.store.put('delivery:' + attempt_id, attempt)
            return attempt
