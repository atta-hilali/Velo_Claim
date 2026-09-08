from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from lxml import etree

from velo_claim.api.app import create_app
from velo_claim.builders.claim.builder import ClaimBuilderModule
from velo_claim.builders.prior_auth.builder import PAClaimBuilderModule
from velo_claim.checks.orchestrator import calculate_validation_report
from velo_claim.core.container import build_default_container
from velo_claim.core.enums import Severity
from velo_claim.core.models import CheckIssue, CheckResult
from velo_claim.examples.demo_inputs import abu_dhabi_pneumonia_encounter
from velo_claim.pipeline import run_full_pipeline
from velo_claim.submission.service import SubmissionService, digest
from velo_claim.submission.shafafiya import ShafafiyaAdapter, ShafafiyaSettings, SubmissionError, TransportUnknown, parse_xml
from velo_claim.submission.store import MemorySubmissionStore

TOKEN = 'test-reviewer-token-with-at-least-32-characters'


class TestJournal(MemorySubmissionStore):
    """Test double for exercising network paths without a database/network."""
    __test__ = False
    durable = True


class Gateway:
    def __init__(self):
        self.uploads = []
        self.reply = {'code': 0, 'transaction_id': 'TX-001', 'message': '', 'error_report': None}
        self.files = []
        self.downloads = {}
        self.acks = []
        self.search = []
        self.failure = None

    def connect(self): pass

    def upload(self, payload, filename):
        self.uploads.append((payload, filename))
        if self.failure: raise self.failure
        return self.reply

    def pending(self, prior_auth=False): return self.files if prior_auth else []
    def download(self, file_id): return 'response.xml', self.downloads[file_id]
    def acknowledge(self, file_id): self.acks.append(file_id)
    def search_sent(self, *args): return self.search


@pytest.fixture
def setup(monkeypatch):
    for key in ('USE_CODING_LLM', 'USE_VALIDATION_LLM', 'ELIGIBILITY_SUBMIT_TO_PAYER'):
        monkeypatch.setenv(key, 'false')
    services = build_default_container()
    services.submission_store = TestJournal()
    state = run_full_pipeline(abu_dhabi_pneumonia_encounter(), container=services)
    gateway = Gateway()
    svc = SubmissionService(services, ShafafiyaSettings(mode='pte', sender_id='MF2057'), gateway)
    return svc, services, state, gateway


def approved(svc, kind='claim', entity='CLM-CLEAN-AUH-001'):
    target = svc.target(kind, entity)
    return svc.approve(kind, entity, target['payload_hash'], 'reviewer-1', 'Reviewed source, warnings and exact payload.')


def pa_request(services, state):
    module = PAClaimBuilderModule(repository=services.repository, object_store=services.object_store,
        submission_store=services.submission_store)
    return module.build(state, ['99213'])['pa_request_id']


def pa_xml(attempt, payment='450', code='99213', payer='A001', reference='AUTH-REAL-001', quantity='1'):
    return f'''<Prior.Authorization><Header><SenderID>{payer}</SenderID><ReceiverID>MF2057</ReceiverID>
    <TransactionDate>16/06/2026 12:00</TransactionDate><RecordCount>1</RecordCount><DispositionFlag>PTE_SUBMIT</DispositionFlag></Header>
    <Authorization><ID>{attempt['wire_id']}</ID><IDPayer>{reference}</IDPayer><Start>01/06/2026 00:00</Start><End>01/07/2026 00:00</End>
    <Activity><ID>ACT-001</ID><Type>3</Type><Code>{code}</Code><Quantity>{quantity}</Quantity><Net>450</Net><PaymentAmount>{payment}</PaymentAmount></Activity>
    </Authorization></Prior.Authorization>'''


def response_for(svc, attempt, **kwargs):
    root = parse_xml(pa_xml(attempt, **kwargs))
    req = parse_xml(svc.objects.get_text(attempt['object_uri']))
    root.find('Authorization/Activity/ID').text = req.findtext('Authorization/Activity/ID')
    return etree.tostring(root)


def test_real_payload_approval_and_delivery_are_separate(setup):
    svc, services, _, gw = setup
    approval = approved(svc)
    assert 'PTE_SUBMIT' in svc.objects.get_text(approval['object_uri'])
    assert not gw.uploads
    delivery = svc.deliver(approval['id'], 'reviewer-1')
    assert delivery['delivery_status'] == 'ACKNOWLEDGED'
    assert delivery['payer_decision'] == 'PENDING'
    assert delivery['transaction_id'] == 'TX-001'
    assert len(gw.uploads) == 1
    assert digest(gw.uploads[0][0]) == approval['payload_hash']
    assert services.repository.get_claim_detail(delivery['entity_id'])['status'] == 'SUBMITTED'


def test_unapproved_and_stale_preview_fail(setup):
    svc, _, _, gw = setup
    with pytest.raises(SubmissionError, match='Approval record'):
        svc.deliver('missing', 'reviewer-1')
    with pytest.raises(SubmissionError) as exc:
        svc.approve('claim', 'CLM-CLEAN-AUH-001', '0'*64, 'reviewer-1', 'reviewed')
    assert exc.value.code == 'STALE_PAYLOAD'
    assert not gw.uploads


def test_error_at_score_80_is_not_ready():
    issue = CheckIssue('ERROR', Severity.ERROR, 'CODING', 'diagnosis', 'Missing', 'Fix', 20)
    report = calculate_validation_report('C', [CheckResult('CODING', 'FAIL', [issue])])
    assert report.score == 80
    assert report.status == 'NEEDS_REVIEW'


def test_report_must_match_exact_hash_and_version(setup):
    svc, services, _, _ = setup
    report = next(iter(services.repository.validation_reports.values()))
    report['report']['payload_hash'] = '0'*64
    with pytest.raises(SubmissionError) as exc: approved(svc)
    assert exc.value.code == 'STALE_VALIDATION'


def test_payload_changed_after_approval_blocks(setup):
    svc, services, state, gw = setup
    approval = approved(svc)
    ClaimBuilderModule(repository=services.repository, object_store=services.object_store,
        kg_client=services.kg_client, payer_rule_loader=services.payer_rule_loader).build(state)
    with pytest.raises(SubmissionError): svc.deliver(approval['id'], 'reviewer-1')
    assert not gw.uploads


def test_snapshot_tampering_blocks(setup):
    svc, _, _, gw = setup
    approval = approved(svc)
    svc.objects.objects[approval['object_uri']]['value'] = 'tampered'
    with pytest.raises(SubmissionError): svc.deliver(approval['id'], 'reviewer-1')
    assert not gw.uploads


def test_duplicate_concurrent_calls_only_upload_once(setup):
    svc, _, _, gw = setup
    approval = approved(svc)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: svc.deliver(approval['id'], 'reviewer-1'), range(6)))
    assert len(gw.uploads) == 1
    assert len({r['id'] for r in results}) == 1


def test_timeout_never_blindly_retries_and_reconciles(setup):
    svc, _, _, gw = setup
    approval = approved(svc)
    gw.failure = TransportUnknown()
    attempt = svc.deliver(approval['id'], 'reviewer-1')
    assert attempt['delivery_status'] == 'DELIVERY_UNKNOWN'
    assert svc.deliver(approval['id'], 'reviewer-1')['id'] == attempt['id']
    assert svc.reconcile(attempt['id'])['delivery_status'] == 'DELIVERY_UNKNOWN'
    assert len(gw.uploads) == 1
    gw.search = [{'FileID':'FOUND', 'FileName':attempt['filename'], 'SenderID':'MF2057', 'ReceiverID':'A001'}]
    gw.downloads['FOUND'] = svc.objects.get_text(attempt['object_uri']).encode()
    assert svc.reconcile(attempt['id'])['delivery_status'] == 'ACKNOWLEDGED'
    assert len(gw.uploads) == 1


def test_reconciliation_requires_exact_bytes(setup):
    svc, _, _, gw = setup
    gw.failure = TransportUnknown()
    attempt = svc.deliver(approved(svc)['id'], 'reviewer-1')
    gw.search = [{'FileID':'FOUND', 'FileName':attempt['filename'], 'SenderID':'MF2057', 'ReceiverID':'A001'}]
    gw.downloads['FOUND'] = b'not the same payload'
    assert svc.reconcile(attempt['id'])['delivery_status'] == 'DELIVERY_UNKNOWN'


@pytest.mark.parametrize('code', [-1, -2, -3, -7, -12])
def test_definitive_upload_rejections(setup, code):
    svc, _, _, gw = setup
    gw.reply.update(code=code, transaction_id=None)
    attempt = svc.deliver(approved(svc)['id'], 'reviewer-1')
    assert attempt['delivery_status'] == 'REJECTED'
    assert attempt['payer_decision'] == 'PENDING'


def test_rejected_retry_is_explicit(setup):
    svc, _, _, gw = setup
    approval = approved(svc)
    gw.reply.update(code=-1, transaction_id=None)
    attempt = svc.deliver(approval['id'], 'reviewer-1')
    svc.retry_rejected(attempt['id'], 'reviewer-1', 'Credentials corrected')
    gw.reply.update(code=0, transaction_id='TX-RETRY')
    again = svc.deliver(approval['id'], 'reviewer-1')
    assert again['id'] != attempt['id']
    assert len(gw.uploads) == 2


@pytest.mark.parametrize('reply', [{'code':0,'transaction_id':None}, {'code':-4,'transaction_id':None}])
def test_indeterminate_platform_result_stays_unknown(setup, reply):
    svc, _, _, gw = setup
    gw.reply.update(reply)
    assert svc.deliver(approved(svc)['id'], 'reviewer-1')['delivery_status'] == 'DELIVERY_UNKNOWN'


def test_real_delivery_requires_durable_store(setup):
    svc, services, _, gw = setup
    services.submission_store.durable = False
    with pytest.raises(SubmissionError) as exc: svc.deliver(approved(svc)['id'], 'reviewer-1')
    assert exc.value.code == 'DURABLE_STORAGE_REQUIRED'
    assert not gw.uploads


def test_manual_export_does_not_fake_submission(setup):
    svc, services, _, gw = setup
    svc.settings = replace(svc.settings, mode='manual')
    attempt = svc.deliver(approved(svc)['id'], 'reviewer-1', manual=True)
    assert attempt['delivery_status'] == 'EXPORTED'
    assert not gw.uploads
    assert services.repository.get_claim_detail(attempt['entity_id'])['status'] == 'READY_TO_SUBMIT'


def test_pa_response_rebuilds_claim_and_requires_fresh_approval(setup):
    svc, services, state, gw = setup
    request_id = pa_request(services, state)
    attempt = svc.deliver(approved(svc, 'prior_auth', request_id)['id'], 'reviewer-1')
    original_version = services.repository.latest_claim_payload(state['claim']['claim_id'])['version']
    content = response_for(svc, attempt)
    event = svc.import_response(content, 'FILE-1', 'reviewer-1')
    assert event['status'] == 'PROCESSED', event
    detail = services.repository.get_claim_detail(state['claim']['claim_id'])
    assert detail['canonical_claim']['pre_auth_ref'] == 'AUTH-REAL-001'
    assert detail['claim_payload']['version'] == original_version + 1
    assert 'AUTH-REAL-001' in svc.objects.get_text(detail['claim_payload']['object_uri'])
    assert len(gw.uploads) == 1  # PA only; no automatic claim delivery.
    duplicate = svc.import_response(content, 'FILE-1-again', 'reviewer-1')
    assert duplicate['id'] == event['id']
    assert services.repository.latest_claim_payload(state['claim']['claim_id'])['version'] == original_version + 1


@pytest.mark.parametrize('changes', [{'quantity':'0.5'}, {'payment':'1'}, {'reference':''}])
def test_partial_or_unknown_pa_never_rebuilds(setup, changes):
    svc, services, state, _ = setup
    attempt = svc.deliver(approved(svc, 'prior_auth', pa_request(services,state))['id'], 'reviewer-1')
    event = svc.import_response(response_for(svc, attempt, **changes), 'FILE-2', 'reviewer-1')
    assert event['status'] == 'PROCESSED'
    assert services.repository.get_claim_detail(state['claim']['claim_id'])['status'] == 'NEEDS_REVIEW'
    assert services.repository.latest_claim_payload(state['claim']['claim_id'])['version'] == 1


def test_wrong_payer_and_unmatched_code_quarantined(setup):
    svc, services, state, _ = setup
    attempt = svc.deliver(approved(svc, 'prior_auth', pa_request(services,state))['id'], 'reviewer-1')
    for changes in ({'payer':'WRONG'}, {'code':'WRONG'}):
        event = svc.import_response(response_for(svc, attempt, **changes), 'FILE-WRONG', 'reviewer-1')
        assert event['status'] == 'NEEDS_REVIEW'
    assert not services.repository.prior_auth_responses


def test_poll_acknowledges_only_after_processing(setup):
    svc, services, state, gw = setup
    attempt = svc.deliver(approved(svc, 'prior_auth', pa_request(services,state))['id'], 'reviewer-1')
    gw.files = [{'FileID':'GOOD','ReceiverID':'MF2057','SenderID':'A001'}, {'FileID':'BAD','ReceiverID':'MF2057','SenderID':'A001'}]
    gw.downloads = {'GOOD':response_for(svc, attempt), 'BAD':b'<unsupported/>'}
    results = svc.poll()
    assert gw.acks == ['GOOD']
    assert results[1]['status'] == 'NEEDS_REVIEW'
    svc.poll()  # Replay does not duplicate response/application.
    assert len(services.repository.prior_auth_responses) == 1


def test_remittance_is_recorded_without_inventing_approval(setup):
    svc, services, _, _ = setup
    attempt = svc.deliver(approved(svc)['id'], 'reviewer-1')
    xml = f'''<Remittance.Advice><Header><SenderID>A001</SenderID><ReceiverID>MF2057</ReceiverID>
    <TransactionDate>16/06/2026 12:00</TransactionDate><RecordCount>1</RecordCount><DispositionFlag>PTE_RESPONSE</DispositionFlag></Header>
    <Claim><ID>{attempt["wire_id"]}</ID><IDPayer>PC1</IDPayer><PaymentReference>PAY-1</PaymentReference>
    <Activity><ID>ACT-AUH-001</ID><Start>15/06/2026 09:00</Start><Type>3</Type><Code>99213</Code>
    <Quantity>1</Quantity><Net>450</Net><PaymentAmount>450</PaymentAmount></Activity></Claim></Remittance.Advice>'''
    result = svc.import_response(xml, 'REM1', 'reviewer-1')
    assert result['status'] == 'PROCESSED'
    assert svc.get_delivery(attempt['id'])['payer_decision'] == 'ADJUDICATED_NEEDS_REVIEW'


def test_invalid_remittance_is_quarantined(setup):
    svc, _, _, _ = setup
    result = svc.import_response(
        '<Remittance.Advice><Header><SenderID>A001</SenderID></Header></Remittance.Advice>',
        'REM-BAD',
        'reviewer-1',
    )
    assert result['status'] == 'NEEDS_REVIEW'
    assert result['error_code'] == 'RESPONSE_PARTY_MISMATCH'


def test_dtd_rejected():
    with pytest.raises(SubmissionError): parse_xml('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')


def test_soap_contract_and_return_codes():
    calls=[]
    class Soap:
        def UploadTransaction(self, **kwargs):
            calls.append(kwargs)
            return {'UploadTransactionResult':1,'TransactionID':'T1','errorMessage':'warning','errorReport':b'error xml'}
        def GetNewPriorAuthorizationTransactions(self, **kwargs):
            return {'GetNewPriorAuthorizationTransactionsResult':2,'foundTransactions':'<Files/>'}
    adapter = ShafafiyaAdapter(ShafafiyaSettings(login='user',password='secret',sender_id='MF2057'), Soap())
    assert adapter.upload(b'<payload/>','file.xml')['transaction_id'] == 'T1'
    assert calls[0] == {'login':'user','pwd':'secret','fileContent':b'<payload/>','fileName':'file.xml'}
    assert adapter.pending(True) == []


def test_soap_timeout_does_not_expose_credentials():
    class Soap:
        def UploadTransaction(self, **kwargs): raise TimeoutError('secret patient payload')
    with pytest.raises(TransportUnknown) as exc:
        ShafafiyaAdapter(ShafafiyaSettings(), Soap()).upload(b'x','x.xml')
    assert 'secret' not in str(exc.value)


def test_api_blocks_legacy_bypasses_and_requires_reviewer(setup, monkeypatch):
    _, services, _, _ = setup
    monkeypatch.setenv('VELO_SUBMISSION_REVIEWERS', '{"'+TOKEN+'":"named-reviewer"}')
    monkeypatch.setenv('VELO_SUBMISSION_MODE', 'manual')
    monkeypatch.setenv('SHAFAFIYA_SENDER_ID','MF2057')
    client = TestClient(create_app(services))
    cid='CLM-CLEAN-AUH-001'
    assert client.post(f'/claims/{cid}/submit',json={'approval_id':'x'}).status_code == 401
    headers={'Authorization':'Bearer '+TOKEN}
    assert client.patch(f'/claims/{cid}/status',json={'status':'submitted'},headers=headers).status_code == 409
    assert client.post(f'/claims/{cid}/actions/approve_submit',json={},headers=headers).status_code == 409
    assert client.post(f'/prior-auth/pa/simulate-submit',json={},headers=headers).status_code == 410
    assert client.post(f'/webhooks/payer/{cid}',json={'decision':'approved'},headers=headers).status_code == 410
    preview=client.get(f'/submission/claim/{cid}/preview',headers=headers)
    assert preview.status_code == 200, preview.text
    approval=client.post(f'/submission/claim/{cid}/approve',json={'payload_hash':preview.json()['payload_hash'],'note':'Reviewed','actor':'spoof'},headers=headers)
    assert approval.json()['actor'] == 'named-reviewer'
    export=client.post('/submissions/export',json={'approval_id':approval.json()['id']},headers=headers)
    assert export.status_code == 200
    assert export.headers['X-Submission-ID']
    assert b'PTE_SUBMIT' in export.content


def test_api_manual_actions_use_supported_audit_event_types(setup, monkeypatch):
    _, services, _, _ = setup
    monkeypatch.setenv('VELO_SUBMISSION_REVIEWERS', '{"'+TOKEN+'":"named-reviewer"}')
    client = TestClient(create_app(services))
    headers = {'Authorization': 'Bearer ' + TOKEN}
    claim_id = 'CLM-CLEAN-AUH-001'

    status_response = client.patch(
        f'/claims/{claim_id}/status',
        json={'status': 'NEEDS_REVIEW', 'reason': 'Manual review'},
        headers=headers,
    )
    action_response = client.post(
        f'/claims/{claim_id}/actions/escalate',
        json={'reason': 'Supervisor review'},
        headers=headers,
    )

    assert status_response.status_code == 200, status_response.text
    assert action_response.status_code == 200, action_response.text
    manual_events = [
        event
        for event in services.repository.audit_events
        if event.get('payload', {}).get('event_name') in {'STATUS_UPDATED', 'ACTION_REQUESTED'}
    ]
    assert len(manual_events) == 2
    assert {event['event_type'] for event in manual_events} == {'NODE_EXIT'}


def test_restart_after_journaled_send_does_not_resend(setup):
    svc, services, _, gw = setup
    approval = approved(svc)
    attempt = svc.deliver(approval['id'], 'reviewer-1')
    attempt['delivery_status'] = 'SUBMITTING'  # Crash before acknowledgement persisted.
    svc.store.put('delivery:' + attempt['id'], attempt)
    restarted = SubmissionService(services, svc.settings, gw)
    assert restarted.deliver(approval['id'], 'reviewer-1')['delivery_status'] == 'SUBMITTING'
    assert len(gw.uploads) == 1


def test_acknowledgement_replay_repairs_interrupted_status_update(setup, monkeypatch):
    svc, services, _, gw = setup
    approval = approved(svc)
    original = services.repository.update_claim_status
    monkeypatch.setattr(services.repository, 'update_claim_status', lambda *args: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError): svc.deliver(approval['id'], 'reviewer-1')
    monkeypatch.setattr(services.repository, 'update_claim_status', original)
    svc.deliver(approval['id'], 'reviewer-1')
    assert len(gw.uploads) == 1
    assert services.repository.get_claim_detail('CLM-CLEAN-AUH-001')['status'] == 'SUBMITTED'


def test_changed_claim_does_not_receive_pa_automatically(setup):
    svc, services, state, _ = setup
    attempt = svc.deliver(approved(svc, 'prior_auth', pa_request(services, state))['id'], 'reviewer-1')
    changed = copy.deepcopy(state)
    changed['source_context']['patient']['id'] = 'different-patient'
    ClaimBuilderModule(repository=services.repository, object_store=services.object_store,
        kg_client=services.kg_client, payer_rule_loader=services.payer_rule_loader).build(changed)
    event = svc.import_response(response_for(svc, attempt), 'changed-context', 'reviewer-1')
    assert event['status'] == 'NEEDS_REVIEW'
    assert event['error_code'] == 'CLAIM_CHANGED'
    assert services.repository.get_claim_detail(state['claim']['claim_id'])['status'] == 'NEEDS_REVIEW'
    assert services.repository.latest_claim_payload(state['claim']['claim_id'])['version'] == 2


def test_expired_approval_and_different_facility_block(setup):
    svc, _, _, gw = setup
    approval = approved(svc)
    approval['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    svc.store.put('approval:' + approval['id'], approval)
    with pytest.raises(SubmissionError) as exc: svc.deliver(approval['id'], 'reviewer-1')
    assert exc.value.code == 'APPROVAL_EXPIRED'
    svc.settings = replace(svc.settings, sender_id='OTHER')
    with pytest.raises(SubmissionError) as exc: svc.deliver(approval['id'], 'reviewer-1')
    assert exc.value.code == 'ENVIRONMENT_CHANGED'
    assert not gw.uploads


def test_production_blocked_until_live_validation_is_available(setup):
    svc, _, _, gw = setup
    svc.settings = replace(svc.settings, mode='production', production_enabled=True)
    with pytest.raises(SubmissionError) as exc: approved(svc)
    assert exc.value.code == 'PRODUCTION_NOT_READY'
    assert not gw.uploads


def test_api_manual_receipt_and_recovery_listing(setup, monkeypatch):
    _, services, _, _ = setup
    monkeypatch.setenv('VELO_SUBMISSION_REVIEWERS', '{"'+TOKEN+'":"named-reviewer"}')
    monkeypatch.setenv('VELO_SUBMISSION_MODE', 'manual')
    monkeypatch.setenv('SHAFAFIYA_SENDER_ID', 'MF2057')
    client = TestClient(create_app(services))
    headers = {'Authorization': 'Bearer ' + TOKEN}
    preview = client.get('/submission/claim/CLM-CLEAN-AUH-001/preview', headers=headers).json()
    approval = client.post('/submission/claim/CLM-CLEAN-AUH-001/approve', headers=headers,
        json={'payload_hash': preview['payload_hash'], 'note': 'Reviewed'}).json()
    export = client.post('/submissions/export', headers=headers, json={'approval_id': approval['id']})
    deliveries = client.get('/submissions', headers=headers).json()['deliveries']
    assert len(deliveries) == 1
    assert deliveries[0]['filename'] in export.headers['Content-Disposition']
    assert 'snapshot' not in deliveries[0] and 'object_uri' not in deliveries[0]
    receipt = client.post('/submissions/' + deliveries[0]['id'] + '/manual-receipt', headers=headers,
        json={'transaction_id': 'PORTAL-1', 'note': 'Portal acknowledged this filename'}).json()
    assert receipt['payer_decision'] == 'PENDING'
    assert receipt['receipt_source'] == 'MANUAL_RECORDED'
    assert receipt['receipt_actor'] == 'named-reviewer'
    assert client.get('/claims/CLM-CLEAN-AUH-001').json()['status'] == 'submitted'


def test_postgres_journal_and_pa_continuation(monkeypatch):
    """Opt-in real DB gate; isolated schema, synthetic data, no payer calls."""
    import os
    from pathlib import Path
    from uuid import uuid4
    dsn = os.getenv('VELO_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Set VELO_TEST_POSTGRES_DSN to a dedicated PostgreSQL test database.')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from velo_claim.storage.postgres import PostgresRepository
    from velo_claim.submission.store import PostgresSubmissionStore
    schema = 'velo_submission_test_' + uuid4().hex
    repository = None
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            isolated = make_conninfo(dsn, options=f'-c search_path={schema},public')
            with psycopg.connect(isolated) as connection:
                for migration in sorted((Path(__file__).resolve().parents[1] / 'velo_claim/migrations').glob('*.sql')):
                    connection.execute(migration.read_text())
            for key in ('USE_CODING_LLM', 'USE_VALIDATION_LLM', 'ELIGIBILITY_SUBMIT_TO_PAYER'):
                monkeypatch.setenv(key, 'false')
            services = build_default_container()
            repository = PostgresRepository(isolated)
            services.repository = repository
            services.submission_store = PostgresSubmissionStore(repository)
            state = run_full_pipeline(abu_dhabi_pneumonia_encounter(), container=services)
            gw = Gateway()
            svc = SubmissionService(services, ShafafiyaSettings(mode='pte', sender_id='MF2057'), gw)
            request_id = pa_request(services, state)
            approval = approved(svc, 'prior_auth', request_id)
            with ThreadPoolExecutor(max_workers=6) as pool:
                attempts = list(pool.map(lambda _: svc.deliver(approval['id'], 'reviewer-1'), range(6)))
            assert len(gw.uploads) == 1
            event = svc.import_response(response_for(svc, attempts[0]), 'PG-RESPONSE', 'reviewer-1')
            assert event['status'] == 'PROCESSED', event
            assert repository.get_claim_detail(state['claim']['claim_id'])['canonical_claim']['pre_auth_ref'] == 'AUTH-REAL-001'
            services.submission_store = PostgresSubmissionStore(repository)
            restarted = SubmissionService(services, svc.settings, gw)
            assert restarted.get_delivery(attempts[0]['id'])['payer_decision'] == 'approved'
            assert restarted.import_response(response_for(svc, attempts[0]), 'REPLAY', 'reviewer-1')['id'] == event['id']
            with repository._connect() as connection:
                assert connection.execute('SELECT count(*) AS n FROM prior_auth_response').fetchone()['n'] == 1
        finally:
            if repository is not None and repository._pool is not None:
                repository._pool.close()
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_repeated_procedure_codes_preserve_each_charge_once():
    from velo_claim.builders.prior_auth.canonical import build_pa_canonical_form
    form = build_pa_canonical_form({'canonical_claim': {
        'claim_id': 'C', 'procedures': [{'code': '99213'}, {'code': '99213'}],
        'line_items': [{'id': 'A1', 'code': '99213', 'net': 100}, {'id': 'A2', 'code': '99213', 'net': 200}]
    }}, ['99213'])
    assert [p['id'] for p in form.procedures] == ['A1', 'A2']
    assert sum(p['net'] for p in form.procedures) == 300
