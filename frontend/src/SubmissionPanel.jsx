import React, { useState } from 'react';
import { submissionRequest, setReviewerToken } from './designApi.js';

export default function SubmissionPanel({ claimId, onDelivered }) {
  const [token, setToken] = useState('');
  const [kind, setKind] = useState('claim');
  const [requestId, setRequestId] = useState('');
  const [preview, setPreview] = useState(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [result, setResult] = useState(null);
  const [history, setHistory] = useState([]);
  const [receipt, setReceipt] = useState('');
  const [responseXml, setResponseXml] = useState('');
  const [externalId, setExternalId] = useState('');
  const entity = kind === 'claim' ? claimId : requestId.trim();
  const run = async (fn) => {
    setBusy(true); setMessage('');
    try { await fn(); } catch (e) { setMessage(e.message); }
    finally { setBusy(false); }
  };
  const inspect = () => run(async () => {
    setReviewerToken(token); setPreview(null); setResult(null);
    const value = await submissionRequest(`/submission/${kind}/${encodeURIComponent(entity)}/preview`);
    setPreview(value);
  });
  const approve = () => run(async () => {
    const approval = await submissionRequest(`/submission/${kind}/${encodeURIComponent(entity)}/approve`, {
      method: 'POST', body: JSON.stringify({payload_hash: preview.payload_hash, note}),
    });
    if (preview.mode === 'manual') {
      const response = await submissionRequest('/submissions/export', {
        method: 'POST', body: JSON.stringify({approval_id: approval.id}),
      }, true);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'approved-shafafiya.xml'; link.click();
      URL.revokeObjectURL(url);
      setResult({delivery_status: 'EXPORTED', id: response.headers.get('X-Submission-ID')});
      setMessage('XML exported. Upload it in the authorized portal; no electronic submission has occurred.');
    } else {
      const path = kind === 'claim' ? `/claims/${encodeURIComponent(preview.entity_id)}/submit` : `/prior-auth/${encodeURIComponent(preview.entity_id)}/submit`;
      const delivery = await submissionRequest(path, {method: 'POST', body: JSON.stringify({approval_id: approval.id})});
      setResult(delivery);
      setMessage(delivery.delivery_status === 'ACKNOWLEDGED' ? 'Upload acknowledged. Payer decision is pending.' : 'Check the delivery status below before taking another action.');
      if (delivery.delivery_status === 'ACKNOWLEDGED') await onDelivered?.();
    }
    setPreview(null);
  });
  return <div style={{display:'grid', gap:12}}>
    <label>Reviewer access token<input type="password" autoComplete="off" value={token}
      onChange={e => {setToken(e.target.value); setPreview(null); setReviewerToken(e.target.value);}}
      style={{display:'block', width:'100%'}} /></label>
    <label>Request type <select value={kind} onChange={e => {setKind(e.target.value); setPreview(null);}}>
      <option value="claim">Claim</option><option value="prior_auth">Prior authorization</option>
    </select></label>
    {kind === 'prior_auth' && <label>Prior authorization request ID<input value={requestId}
      onChange={e => {setRequestId(e.target.value); setPreview(null);}} /></label>}
    <button disabled={busy || !token || !entity} onClick={inspect}>Review exact payload</button>
    {preview && <>
      <p>Environment: <b>{preview.mode}</b> · Version: {preview.version} · Receiver: {preview.payer_id}</p>
      <small style={{overflowWrap:'anywhere'}}>Payload SHA-256: {preview.payload_hash}</small>
      <textarea aria-label="Payload for approval" readOnly value={preview.payload} rows={12} style={{width:'100%', fontFamily:'monospace'}} />
      <label>Approval note<textarea value={note} onChange={e => setNote(e.target.value)} style={{width:'100%'}} /></label>
      <button disabled={busy || !note.trim()} onClick={approve}>{preview.mode === 'manual' ? 'Approve & export XML' : 'Approve & send to Shafafiya'}</button>
    </>}
    <details><summary>Delivery history and payer responses</summary>
      <button disabled={busy || !token} onClick={() => run(async () => {
        const data = await submissionRequest('/submissions');
        setHistory(data.deliveries.filter(a => a.claim_id === claimId));
      })}>Load delivery history</button>
      {history.map(a => <p key={a.id}><button onClick={() => setResult(a)}>{a.kind} · {a.delivery_status} · {a.id}</button></p>)}
      <button disabled={busy || !token} onClick={() => run(async () => {
        const data = await submissionRequest('/submissions/responses/poll', {method:'POST'});
        setMessage(JSON.stringify(data.results)); await onDelivered?.();
      })}>Retrieve payer responses</button>
      <p>Manual mode: select the XML response downloaded from the authorized test portal.</p>
      <input aria-label="Response XML file" type="file" accept=".xml,application/xml,text/xml" onChange={e => run(async () => {
        const file = e.target.files?.[0]; if (!file) return;
        if (file.size > 20 * 1024 * 1024) throw new Error('Response exceeds 20 MB.');
        setResponseXml(await file.text()); setExternalId(file.name);
      })} />
      <label>Portal response reference<input value={externalId} onChange={e => setExternalId(e.target.value)} /></label>
      <button disabled={busy || !token || !responseXml || !externalId} onClick={() => run(async () => {
        const event = await submissionRequest('/submissions/responses/import', {method:'POST', body:JSON.stringify({xml:responseXml, external_id:externalId})});
        setMessage(`Response ${event.status}${event.error_code ? ': ' + event.error_code : ''}`); await onDelivered?.();
      })}>Import response</button>
    </details>
    {message && <p role="status">{message}</p>}
    {result && <div><b>{result.delivery_status}</b><p>Delivery ID: {result.id}</p>
      <p>Payer decision: {result.payer_decision || 'PENDING'}</p>
      {result.transaction_id && <p>Transaction: {result.transaction_id}</p>}
      <button disabled={busy} onClick={() => run(async () => setResult(await submissionRequest(`/submissions/${result.id}`)))}>Refresh status</button>
      {result.delivery_status === 'EXPORTED' && <>
        <label>Portal transaction ID<input value={receipt} onChange={e => setReceipt(e.target.value)} /></label>
        <label>Receipt note<input value={note} onChange={e => setNote(e.target.value)} /></label>
        <button disabled={busy || !receipt.trim() || !note.trim()} onClick={() => run(async () => {
          setResult(await submissionRequest(`/submissions/${result.id}/manual-receipt`, {method:'POST',body:JSON.stringify({transaction_id:receipt,note})}));
          await onDelivered?.();
        })}>Record portal upload receipt</button>
      </>}
      {result.delivery_status === 'REJECTED' && <>
        <label>Correction made<input value={note} onChange={e => setNote(e.target.value)} /></label>
        <button disabled={busy || !note.trim()} onClick={() => run(async () => {
          setResult(await submissionRequest(`/submissions/${result.id}/release-rejected`, {method:'POST',body:JSON.stringify({note})}));
          setMessage('Rejection recorded. Review the corrected payload before another attempt.');
        })}>Release rejected attempt</button>
      </>}
      {['DELIVERY_UNKNOWN','SUBMITTING'].includes(result.delivery_status) && <button disabled={busy} onClick={() => run(async () => {
        setResult(await submissionRequest(`/submissions/${result.id}/reconcile`, {method:'POST'}));
      })}>Reconcile delivery</button>}
    </div>}
  </div>;
}
