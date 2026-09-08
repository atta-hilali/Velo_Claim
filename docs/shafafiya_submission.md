# Controlled Shafafiya submission

This change replaces the claim/PA simulation endpoints with an approval-controlled
Shafafiya test integration. It does not activate payer delivery on installation.
The API defaults to disabled; local development and manual test-portal exchange
work without Docker. Network PTE delivery requires persistent PostgreSQL and
object storage. KG and payer-rule validation still use the existing mock/fallback
implementations. **Production submission is explicitly blocked in this release.**

## What changed

| Area | Behavior |
| --- | --- |
| Claim validation | A current successful report must match both payload version and SHA-256; ERROR/CRITICAL issues block submission, regardless of score. |
| PA validation | Requests have unique provider wire IDs, retained continuation context, and XSD validation before approval. A built request is not marked submitted. |
| Approval | A server-authenticated named reviewer approves the exact XML, payer, environment, version, and hash, with a note and a 24-hour expiry. |
| Upload | WSDL-bound SOAP `UploadTransaction` uses the frozen XML and stable filename. Real transaction IDs and diagnostic artifacts are retained. |
| Duplicate prevention | Persistent journal and per-target PostgreSQL advisory locks serialize delivery. Repeated requests return the existing attempt. |
| Uncertain delivery | Timeouts, interrupted uploads, and indeterminate replies remain unknown. Search plus downloaded-byte comparison reconciles delivery; no match never authorizes a retry. |
| Response retrieval | List normal and PA transactions, download XML/ZIP, archive it, correlate parties/request/activity IDs, apply it, then acknowledge the file. |
| Continuation | A fully covered PA can rebuild/revalidate its unchanged linked claim with the payer reference. The resulting claim needs new human approval. |
| Manual exchange | Export the approved PTE XML, record the portal receipt, and import downloaded response XML. Export itself does not mark the claim submitted. |
| Frontend | Exact-payload approval, delivery history, reconciliation, manual receipt/import, response polling, and actual server status replace simulated success/force-submit behavior. |

Upload acknowledgement only confirms platform delivery. `payer_decision` remains
`PENDING` until a matched response arrives. Remittances retain claim/activity
results and require review; this release does not infer blanket claim approval,
settled payment, or complete accounting from them. PA approval is conservative:
all requested activities must match, quantities/payment must cover the request,
and a usable reference and dates must exist. Partial/unknown/denied results and
changed claim context require review.

## Local setup while DGX is down

1. Install `requirements.txt` in your usual Python environment. For the isolated
   backend checks, run `python -m pytest -q`. No Docker or payer credentials are
   needed for deterministic tests.
2. Add the settings from `config/shafafiya.env.example` to your existing root
   `.env` without overwriting existing settings. Choose `VELO_SUBMISSION_MODE=manual`
   and `VELO_CLAIM_STORAGE=memory` for local synthetic-data development.
3. Set `SHAFAFIYA_SENDER_ID` to the facility ID used in the XML. The bundled Abu
   Dhabi example uses `MF2057`; this is fixture data, not an assigned credential.
4. Generate one private access token per reviewer with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Set
   `VELO_SUBMISSION_REVIEWERS` to a JSON object mapping each token to its named
   reviewer identity. Tokens shorter than 32 characters are rejected. Keep the
   actual mapping in environment configuration; never commit it.
5. Start `python -m uvicorn velo_claim.api.app:app --host 127.0.0.1 --port 8002`.
   Configure the frontend's `VITE_API_BASE_URL=http://127.0.0.1:8002`, then run
   `npm ci` and `npm run dev` in `frontend/`.
6. Ingest a synthetic Abu Dhabi encounter through `POST /encounters`. Existing
   encounters need revalidation because older reports do not contain the new
   payload hash. Older PA requests need rebuilding to capture continuation
   context. The `abu_dhabi_pneumonia_encounter()` fixture and tests demonstrate it.
7. Open a claim's **Review Submission** panel, enter the reviewer token, select
   Claim or Prior authorization, and review the XML. For PA, use the canonical
   request UUID returned by `/prior-auth/build` (the preview also resolves a
   display ID). Add the note and approve/export.
8. Preserve the downloaded `vc-<delivery-id>.xml` filename. Upload only to your
   authorized **test** portal. Record its real transaction ID under delivery
   history, then import the actual response XML when available.

Memory storage loses approvals, export tracking and responses on restart.
Use it only for synthetic local exercises. Any actual manual portal exchange
also needs persistent tracking before export; configure the persistent stack
for that workflow. Manual exports currently use `PTE_SUBMIT`, never production.
The access token remains in frontend memory, not local storage; reload clears it.
Other pre-existing read/ingest APIs retain their existing access model, so this
change is not a complete authentication or tenancy solution for the entire app.

## PTE setup when the persistent stack is available

Use authorized PTE credentials, the assigned sender/facility ID, an appropriate
test receiver, and suitable synthetic transactions. Configure:

- `VELO_CLAIM_STORAGE=production` selects PostgreSQL/S3/Redis adapters. This name
  describes storage, not the payer environment.
- `DATABASE_URL`, `REDIS_URL`, `OBJECT_STORE_BUCKET`,
  `OBJECT_STORE_ENDPOINT_URL` if using MinIO, and the existing AWS credential
  settings for the object store. Create the private bucket using your normal
  deployment process.
- `VELO_SUBMISSION_MODE=pte`, `SHAFAFIYA_LOGIN`, `SHAFAFIYA_PASSWORD`,
  `SHAFAFIYA_SENDER_ID`, and `VELO_SUBMISSION_REVIEWERS`.
- Keep `SHAFAFIYA_ENABLE_PRODUCTION=false`. Even setting it true does not remove
  this release's production guard.

Apply migrations once in order, including **both** `002_*` files. Use
`003_shafafiya_release_alignment.sql` as the current 003 migration; it
supersedes the older display-ID-only migration. For an existing database,
apply only migrations not already applied. Migrations 004 and 005 are additive:
004 repairs fresh schemas that lack `prior_auth_response.claim_id`, while 005
creates `payer_submission_journal` and aligns that column for historical
schema variants. Never delete journal or response records to work around an
unknown delivery.

The SOAP endpoint is pinned to
`https://shafafiyapte.doh.gov.ae/v3/webservices.asmx`; its WSDL is loaded lazily.
TLS verification stays enabled, redirects are rejected, and uploads have no
automatic transport retries. `SHAFAFIYA_TIMEOUT_SECONDS` accepts 1–120 seconds.
WSDL/configuration failures occur before an upload attempt; an interrupted
attempt after the journal records `SUBMITTING` must be reconciled.

Use **Retrieve payer responses** or authenticated
`POST /submissions/responses/poll`. This is an explicit poll operation, not a
background scheduler. Your deployment scheduler may invoke it periodically
using a dedicated server credential; this change installs no scheduled job.
`NEEDS_REVIEW`/`RETRY_OR_REVIEW` files remain unacknowledged and archived.
Review `GET /submissions/responses/events`; polling or reimporting the same bytes
resumes recoverable processing without duplicating completed actions.

## API workflow

All endpoints below require `Authorization: Bearer <reviewer-token>`.
`kind` is `claim` or `prior_auth`.

| Method and path | Body / result |
| --- | --- |
| GET `/submission/{kind}/{id}/preview` | Exact transformed, XSD-validated XML plus hash and environment. |
| POST `/submission/{kind}/{id}/approve` | `{ "payload_hash": "<preview hash>", "note": "<review note>" }` → approval ID. |
| POST `/claims/{id}/submit` | `{ "approval_id": "<id>" }` → actual delivery status. |
| POST `/prior-auth/{request_uuid}/submit` | Same body; uses the canonical entity ID in the approval. |
| POST `/submissions/export` | Same body; manual mode only; XML attachment and `X-Submission-ID`. |
| GET `/submissions` | Delivery history for the configured environment/facility. |
| GET `/submissions/{id}` | Delivery status, actual transaction reference, and separate payer decision. |
| POST `/submissions/{id}/reconcile` | Search/download verification; never blindly resends. |
| POST `/submissions/{id}/release-rejected` | `{ "note": "<correction made>" }`; only definitive rejection is releasable. |
| POST `/submissions/{id}/manual-receipt` | `{ "transaction_id": "<portal ID>", "note": "<evidence>" }`; explicitly marked manual-recorded. |
| POST `/submissions/responses/import` | `{ "xml": "<downloaded XML>", "external_id": "<portal reference>" }`; manual mode. |
| POST `/submissions/responses/poll` | Fetch/process/acknowledge received transactions; PTE mode. |
| GET `/submissions/responses/events` | Processing/review status of archived response files. |

A released rejection can be retried only after fixing the underlying problem;
all current validation/approval checks still run. An unchanged, unexpired
approval can be reused; changed bytes need a fresh preview and approval.
There is deliberately no release path for unknown/acknowledged deliveries.
An amended or additional payer decision is retained for manual reconciliation,
not silently substituted for the previous decision.

Legacy simulated PA submission, simulated cancellation and generic payer webhook
routes return HTTP 410. Status/action endpoints cannot set readiness/submitted
states or force submission. They still support authenticated hold/review actions.
The new journal is authoritative for delivery; historical `submission_attempt`
rows are preserved and are not converted into real transaction acknowledgements.

## Verification and remaining activation gates

`python -m pytest -q` exercises deterministic backend/API tests, including
concurrency, exact approvals, timeout reconciliation, response correlation,
partial PA decisions, continuation, restart recovery and manual receipt handling.
`npm run build` verifies the frontend bundle.

Set `VELO_TEST_POSTGRES_DSN` to a dedicated test database to run the optional
PostgreSQL integration test. It creates and drops a unique test schema, applies
the migrations and exercises the persistent journal and PA continuation. It does
not use a payer or S3. Without that variable it is skipped.

Before relying on an actual PTE exchange, run the PostgreSQL test and the full
PostgreSQL/S3/Redis deployment, verify WSDL binding and credentials against the
live PTE, and exercise accepted/rejected uploads, download/acknowledgement,
restart recovery and at least one matched PA response. These live infrastructure
and payer exchanges were not performed in this coding session. No production
claims were sent. KG integration remains a separate deferred task; see
[kg_integration_plan.md](kg_integration_plan.md).

## Contract references

- [DoH public test environment](https://www.doh.gov.ae/en/shafafiya/dictionary/public-test-environment)
- [DoH Shafafiya web-service technical definition](https://doh.gov.ae/-/media/Feature/shafifya/PTE-XSD/DoH_ShafafiyaPTE_-Web-Services-Technical-definition-document-V3.ashx)
- Bundled `data/schemas/shafafiya/v2.0/ClaimSubmission.xsd`,
  `PriorRequest.xsd`, `PriorAuthorization.xsd`, `RemittanceAdvice.xsd`, and
  `CommonTypes.xsd` from one immutable release.

The published contract guided the adapter; live WSDL interoperability still needs
an authorized PTE check. Remittances are XSD-validated and remain review-only;
there is no automatic payment posting.
