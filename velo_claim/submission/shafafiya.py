from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

PTE_URL = 'https://shafafiyapte.doh.gov.ae/v3/webservices.asmx'
PRODUCTION_URL = 'https://shafafiya.doh.gov.ae/v3/webservices.asmx'
MAX_BYTES = 20 * 1024 * 1024


class SubmissionError(Exception):
    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TransportUnknown(SubmissionError):
    def __init__(self):
        super().__init__('DELIVERY_UNKNOWN', 'Delivery is uncertain. Reconcile before any further submission.', 502)


def parse_xml(content: bytes | str):
    raw = content.encode() if isinstance(content, str) else content
    if len(raw) > MAX_BYTES:
        raise SubmissionError('XML_TOO_LARGE', 'XML exceeds the supported size.', 413)
    try:
        root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
        if root.getroottree().docinfo.doctype:
            raise ValueError('DTD')
        return root
    except (etree.XMLSyntaxError, ValueError):
        raise SubmissionError('INVALID_XML', 'Expected XML without DTDs or external entities.', 422) from None


@dataclass(frozen=True)
class ShafafiyaSettings:
    mode: str = 'disabled'
    sender_id: str = ''
    login: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    timeout: int = 30
    production_enabled: bool = False

    @classmethod
    def from_env(cls):
        value = cls(mode=os.getenv('VELO_SUBMISSION_MODE', 'disabled').lower(),
                    sender_id=os.getenv('SHAFAFIYA_SENDER_ID', ''),
                    login=os.getenv('SHAFAFIYA_LOGIN', ''), password=os.getenv('SHAFAFIYA_PASSWORD', ''),
                    timeout=int(os.getenv('SHAFAFIYA_TIMEOUT_SECONDS', '30')),
                    production_enabled=os.getenv('SHAFAFIYA_ENABLE_PRODUCTION', '').lower() == 'true')
        if value.mode not in {'disabled', 'manual', 'pte', 'production'}:
            raise SubmissionError('CONFIGURATION', 'Unknown submission mode.', 503)
        if value.mode == 'production' and not value.production_enabled:
            raise SubmissionError('CONFIGURATION', 'Production delivery has not been enabled.', 503)
        if not 1 <= value.timeout <= 120:
            raise SubmissionError('CONFIGURATION', 'Timeout must be between 1 and 120 seconds.', 503)
        return value

    @property
    def endpoint(self):
        return PRODUCTION_URL if self.mode == 'production' else PTE_URL


class ShafafiyaAdapter:
    """SOAP calls bound from the official endpoint WSDL, without guessed namespaces.

    No automatic HTTP retry is configured. Upload transport/protocol failures
    are indeterminate, because the server may have committed the transaction.
    The injectable service is used only for contract tests.
    """
    def __init__(self, settings: ShafafiyaSettings, service: Any = None):
        self.settings = settings
        self._service = service

    def connect(self):
        if self._service is not None:
            return
        if self.settings.mode not in {'pte', 'production'}:
            raise SubmissionError('DISABLED', 'Network submission is not enabled.', 503)
        if not all([self.settings.login, self.settings.password, self.settings.sender_id]):
            raise SubmissionError('CONFIGURATION', 'Shafafiya credentials and sender ID must be configured.', 503)
        try:
            import requests
            from zeep import Client, Settings
            from zeep.transports import Transport
            class NoRedirectSession(requests.Session):
                def request(self, method, url, **kwargs):
                    kwargs['allow_redirects'] = False
                    response = super().request(method, url, **kwargs)
                    if 300 <= response.status_code < 400:
                        raise requests.RequestException('Unexpected SOAP redirect')
                    return response
            session = NoRedirectSession()
            transport = Transport(session=session, timeout=self.settings.timeout, operation_timeout=self.settings.timeout)
            client = Client(self.settings.endpoint + '?WSDL', transport=transport,
                            settings=Settings(strict=True, forbid_dtd=True, forbid_entities=True, forbid_external=True))
            # Select a SOAP 1.1 binding from the WSDL; pin the transport address.
            bindings = [b for b in client.wsdl.bindings.values() if b.__class__.__name__ == 'Soap11Binding']
            if not bindings:
                raise ValueError('No SOAP 1.1 binding')
            self._service = client.create_service(bindings[0].name, self.settings.endpoint)
        except Exception:
            raise SubmissionError('CONNECTION_FAILED', 'Could not initialize the Shafafiya WSDL connection.', 503) from None

    def _call(self, operation, **kwargs):
        self.connect()
        try:
            result = getattr(self._service, operation)(login=self.settings.login, pwd=self.settings.password, **kwargs)
            if not isinstance(result, dict):
                from zeep.helpers import serialize_object
                result = serialize_object(result, target_cls=dict)
            code = int(result[operation + 'Result'])
            return {**result, 'code': code}
        except Exception:
            # Do not expose credentials, SOAP request bodies or server traces.
            if operation == 'UploadTransaction':
                raise TransportUnknown() from None
            raise SubmissionError('PAYER_UNAVAILABLE', 'The Shafafiya operation failed; retrieval can be retried.', 502) from None

    def upload(self, payload: bytes, filename: str):
        result = self._call('UploadTransaction', fileContent=payload, fileName=filename)
        return {'code': result['code'], 'transaction_id': result.get('TransactionID'),
                'message': result.get('errorMessage') or '', 'error_report': result.get('errorReport')}

    def pending(self, prior_auth=False):
        operation = 'GetNewPriorAuthorizationTransactions' if prior_auth else 'GetNewTransactions'
        result = self._call(operation, SenderID=self.settings.sender_id)
        self._require_success(result, allow_empty=prior_auth)
        return self._files(result.get('foundTransactions') or result.get('xmlTransactions') or '<Files/>')

    def download(self, file_id):
        result = self._call('DownloadTransactionFile', fileID=file_id)
        self._require_success(result)
        content = result.get('file') or b''
        if isinstance(content, str):
            content = base64.b64decode(content, validate=True)
        if not content or len(content) > MAX_BYTES:
            raise SubmissionError('INVALID_DOWNLOAD', 'Empty or oversized transaction file.', 422)
        return result.get('fileName') or '', content

    def acknowledge(self, file_id):
        self._require_success(self._call('SetTransactionDownloaded', fileID=file_id))

    def search_sent(self, filename, payer_id, prior_auth=False):
        found = []
        for status in (1, 2):
            result = self._call('SearchTransactions', direction=1, callerLicense=self.settings.sender_id,
                ePartner=payer_id, transactionID=16 if prior_auth else 2, transactionStatus=status,
                transactionFileName=filename, transactionFromDate=None, transactionToDate=None,
                minRecordCount=-1, maxRecordCount=-1)
            self._require_success(result)
            found.extend(self._files(result.get('foundTransactions') or '<Files/>'))
        return found

    @staticmethod
    def _require_success(result, allow_empty=False):
        if result['code'] not in ({0, 1, 2} if allow_empty else {0, 1}):
            code = 'AUTHENTICATION_FAILED' if result['code'] == -1 else 'PAYER_REJECTED_OPERATION'
            raise SubmissionError(code, f"Shafafiya returned code {result['code']}.", 502)

    @staticmethod
    def _files(xml):
        root = parse_xml(xml)
        return [dict(e.attrib) for e in root.iter() if etree.QName(e).localname == 'File']
