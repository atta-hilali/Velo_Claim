# Velo Claim MD Conformance Audit

Source brief: `C:\Users\User\Downloads\velo_claim_codex_prompt_v2.md`

Audit date: 2026-06-29

Verification run: `python -m pytest -q` -> `3 passed`

## Executive Summary

The current `velo_claim/` package is now a clean, modular implementation of the
MD architecture. The core pipeline shape is present and runnable:

`FHIR/Input Context -> Claim Preparation -> Claim Validation -> [future Submission]`

The package now includes LangGraph agents, reusable eligibility and prior-auth
subgraphs, route persistence, claim/PA builders, validation checks, audit events,
storage adapters, callback/poll primitives, mock KG, mock payer rules, and a
production-shaped payer-rule circuit breaker.

It is **not yet exact production conformance**. The largest remaining gaps are
live external behavior and official conformance assets:

- Eligibility and prior-auth subgraphs exist, but their payer submit/poll/parse
  paths are still mostly local/manual placeholders.
- Full NPHIES/Shafafiya/eClaimLink validation needs official FHIR validator/IG
  assets and XSD files configured at runtime.
- Real payer portal/API fetchers and OpenJet/NPHIES submission adapters are not
  implemented.
- Idempotency is implemented for callbacks, but not yet wrapped around every
  external touch as required by the MD.
- Claim Preparation has no explicit ErrorHandler branch and performs only a
  payload-presence check; deeper payload conformance currently happens in Claim
  Validation.

## Status Legend

- **Implemented**: present and wired in the package.
- **Mostly implemented**: present and used, but with some production caveat.
- **Partial**: structure exists, but important behavior is stubbed/local/manual.
- **Missing**: not implemented.
- **Manual/config required**: code supports it, but real credentials/assets are
  needed.

## Section 0 - Mental Model

MD requirement:

- Sequential pipeline: Input Resolution -> Claim Preparation -> Claim Validation
  -> future Submission.
- Each major stage is a LangGraph state machine.
- Eligibility and Prior Auth are reusable LangGraph subgraphs.
- One canonical state object is enriched through the flow.

Current code:

- Full in-process pipeline exists in `velo_claim/pipeline.py`.
- FHIR context/input agent exists in `velo_claim/agents/fhir_context_agent.py`.
- Claim preparation agent exists in `velo_claim/agents/claim_preparation.py`.
- Claim validation agent exists in `velo_claim/agents/claim_validation.py`.
- Prior authorization agent exists in `velo_claim/agents/prior_authorization.py`.
- Eligibility subgraph exists in `velo_claim/checks/eligibility_graph.py`.
- Prior-auth subgraph exists in `velo_claim/checks/prior_auth_graph.py`.
- Canonical state defaults exist in `velo_claim/core/models.py`.

Conformance: **Mostly implemented**

Remaining:

- Decide whether `FHIRContextAgent` is officially Agent 1 or an input-resolution
  module. The code treats it as an agent.
- Future submission agent is intentionally out of scope.

## Section 1 - Technology Stack

MD requirement:

- LangGraph, PostgreSQL, S3/MinIO, Redis, mock Neo4j, Redis-backed async jobs,
  FHIR adapters, NPHIES, Shafafiya, eClaimLink.

Current code:

- LangGraph is used by the FHIR, preparation, validation, prior-auth,
  eligibility, and prior-auth check graphs.
- PostgreSQL adapter exists: `velo_claim/storage/postgres.py`.
- S3/MinIO object adapter exists: `velo_claim/storage/object_store.py`.
- Redis cache/lock adapter exists: `velo_claim/storage/redis_cache.py`.
- In-memory dev implementations exist in `velo_claim/storage/memory.py`.
- Mock KG exists in `velo_claim/kg/mock.py`.
- Mock payer rules exist in `velo_claim/rules/mock_loader.py`.
- Live/cached/mock payer-rule loader exists in `velo_claim/rules/live_loader.py`.
- FHIR adapter interface and vendor bridge exist:
  `velo_claim/context/adapters.py`, `velo_claim/context/vendor_bridge.py`.
- Claim builders exist for NPHIES, Shafafiya, and eClaimLink.

Conformance: **Mostly implemented**

Remaining:

- Production mode requires real `DATABASE_URL`, `REDIS_URL`,
  `OBJECT_STORE_BUCKET`, object-store credentials, and optional dependencies
  `psycopg`, `boto3`, and `redis`.
- Real Neo4j driver implementation is not present; only the mock interface is
  present as expected for this sprint.

## Section 2 - Canonical State Object

MD requirement:

- One shared JSON state with claim identity, payloads, route, source context,
  routing context, callback state, next agent, errors, and warnings.
- Errors/warnings append-only.
- Rebuild attempts capped at 3.

Current code:

- `default_state()` defines the expected canonical keys.
- `ClaimError`, `CheckIssue`, `CallbackState`, `SourceContext`,
  `RoutingContext`, and `Route` exist.
- Validation converts report issues into canonical `errors` and `warnings`.
- Rebuild attempts are capped in validation input and fallback rebuild.

Conformance: **Mostly implemented**

Remaining:

- Append-only behavior is mostly respected by convention, but not enforced by a
  central immutable state mechanism.
- Claim payload is still stored both in state and object store for local
  convenience. The MD says large payloads should rely on object URI as source of
  truth.

## Section 3 - Storage Architecture

MD requirement:

- PostgreSQL tables listed in the MD.
- S3 key layout for payloads, reports, PA payloads, callbacks, audit, etc.
- Redis keys for sessions, FHIR context, checkpoints, poll locks, tokens,
  idempotency, bg jobs, eligibility cache, callback locks.
- Mock Neo4j and mock payer rules.
- Circuit-breaker pattern for live payer rules.

Current code:

- Migration exists at `velo_claim/migrations/001_initial_schema.sql`.
- Repository interface and in-memory/Postgres implementations exist.
- Claim payloads use `claims/{claim_id}/versions/{version}/payload.{ext}`.
- Validation reports use `claims/{claim_id}/validation_reports/{report_id}.json`.
- PA payloads use `claims/{claim_id}/prior_auth/pa_payloads/{version}/payload.{ext}`.
- Callback payload archival exists in `velo_claim/fallback/callbacks.py`.
- Audit archival exists in `velo_claim/agents/audit.py`.
- Redis-backed cache supports NX locks and key scanning.
- Payer-rule live/cached/mock circuit breaker exists.

Conformance: **Mostly implemented**

Remaining:

- Repository interface does not yet include first-class methods for every MD
  table/action, for example eligibility upsert, submission attempts, and
  external transaction abstractions.
- Eligibility storage currently uses an in-memory-only attribute path in the
  eligibility subgraph; it is not a repository interface method.
- Some exact S3 paths in the MD are not yet produced, such as prior-auth request
  and response archival paths from the PA parser.
- No schema migration runner is included; migration must be applied manually.

## Section 4 - FHIR Context Layer

MD requirement:

- InputResolver supports RCM upload, Velo Doctor call, and encounter ID.
- FHIR adapter supports Epic, Cerner, Nabidh, TrakCare through one interface.
- FHIR reader dereferences Encounter references and fetches Patient, Coverage,
  Practitioner, Organization, Condition, Procedure, DocumentReference,
  ChargeItem.
- OAuth token cache in Redis.
- Normalize documents into stable internal schema.

Current code:

- Input resolver handles canonical raw state and `encounter_package`.
- FHIR context resolver fetches encounter-dependent resources through
  `AdapterInterface`.
- Vendor bridge wraps the preserved vendor FHIR adapter logic.
- FHIR context agent auto-builds the vendor adapter when FHIR env vars are
  present.
- OAuth tokens can be cached under `token_cache:{provider_id}:{payer_id}`.
- Routing context extraction happens once after context loading.

Conformance: **Partial / Mostly implemented for FHIR JSON**

Remaining:

- RCM multipart, HL7, and CSV parsing are not implemented.
- Velo Doctor-specific envelope is not explicitly modeled beyond
  `encounter_package`.
- Vendor adapters are generic/profile-driven; there are not separate strongly
  typed Epic/Cerner/NABIDH/TrakCare adapter classes in the new interface.
- Document normalization is still basic; attachments are carried as FHIR-ish
  dicts rather than normalized document records with object storage.

## Section 5 - Router

MD requirement:

- Router takes routing context and resolves jurisdiction, claim standard,
  prior-auth standard, eligibility profile, payer-rule profile, and submission
  channel.
- Persists exactly one route decision per claim.
- Downstream agents reload and use persisted route.

Current code:

- Router exists in `velo_claim/routing/router.py`.
- Phase-1 payer registry is used for jurisdiction/standard inference.
- Route decision is persisted by repository.
- In-memory repository rejects conflicting route rewrites.
- PostgreSQL migration has `route_decision.claim_id UNIQUE`.
- Validation checks that exactly one route decision exists.

Conformance: **Implemented**

Remaining:

- Payer/platform profiles can become richer as payer onboarding matures.

## Section 6 - Claim Preparation State Machine

MD requirement:

- `Start -> SupervisorNode -> PrepareClaimNode -> Validate_Claim -> End`, with
  ErrorHandler loop.
- Claim builder has source reader, SourceCodeExtractor, canonical builder, and
  format-specific builder.
- Validate_Claim performs structural schema check.
- Audit on every node.

Current code:

- LangGraph shape exists: supervisor -> prepare_claim -> validate_claim.
- All nodes are audited.
- ClaimBuilderModule builds canonical claim, selects standard-specific builder,
  stores payload in object store, persists payload row and claim version.
- Canonical builder extracts diagnoses, procedures, line items, payer/provider,
  amount, and KG bundling context.
- NPHIES, Shafafiya, eClaimLink claim builders exist.

Conformance: **Partial**

Remaining:

- No explicit ErrorHandler branch.
- `validate_claim` only checks payload presence; full XSD/FHIR/profile checks
  currently run in Claim Validation.
- SourceCodeExtractor is not a separately named module/class. Extraction exists
  inside canonical builder helper functions.
- Format-specific builders are still simplified and need official payer/schema
  hardening before real submission.

## Section 7 - Claim Validation State Machine

MD requirement:

- `normalize_validation_input -> load_route_and_context -> load_payer_rule ->
  parse_payload -> ValidationNode -> CalculateScore -> Router -> End`.
- Fallback loop for payload rebuild, max 3.
- ValidationNode orchestrates metadata, payload conformity, financial,
  eligibility, prior auth, coding, documentation, payer rules, duplicate, and
  readiness checks.

Current code:

- Graph nodes and fallback loop exist in `claim_validation.py`.
- Persisted route is loaded and exactly-one route is enforced.
- Payer rules are loaded.
- Payload validator runs once before validation checks.
- Orchestrator runs the full check list, including eligibility and PA subgraphs.
- Validation report and issues are persisted.
- Full report JSON is stored in object store.
- Validation issues are appended back into canonical `errors` or `warnings`.

Conformance: **Mostly implemented**

Remaining:

- `load_route_and_context` is currently a no-op after normalization.
- Rule set is not explicitly cached under a session/checkpoint Redis key.
- Fallback rebuild rebuilds in-process with `ClaimBuilderModule`; it is not a
  separate handoff back to ClaimPreparationAgent.

## Section 8 - Eligibility Check Subgraph

MD requirement:

- Reusable LangGraph subgraph:
  normalize, check cache, determine requirement, route platform, build payload,
  submit, poll, parse, validate, patch claim, store record, finish.
- Supports cached eligibility and payer-specific TTL.
- DAMAN VOI check.

Current code:

- Reusable LangGraph subgraph exists with the required node names.
- Nodes are audited.
- Eligibility cache key is `eligibility:{patient_id}:{payer_id}:{service_date}`.
- Local coverage validation checks coverage existence, active status, period,
  and DAMAN VOI.
- Eligibility result patches payer eligibility status and benefit summary into
  canonical claim.

Conformance: **Partial**

Remaining:

- `submit_eligibility_request`, `poll_if_async`, and
  `parse_eligibility_response` are local/manual placeholders.
- No real platform-specific eligibility payload builders yet
  (`CoverageEligibilityRequest`, Shafafiya XML, eClaimLink XML).
- No live eligibility adapter submission/polling.
- Repository has no formal `upsert_eligibility_check`; storage is only
  in-memory via an attribute when present.
- WAITING_FOR_PAYER integration is not wired into this subgraph yet.

## Section 9 - Prior Auth Check Subgraph

MD requirement:

- Reusable LangGraph subgraph:
  normalize, determine requirement, validate existing auth, idempotency route,
  PA builder, submit/task, poll, parse response, patch claim, finish.
- Existing auth checks expiration, CPT match, service date, payer match.
- Queued/pended responses enter callback/poll flow.
- Approved auth patches claim and triggers payload rebuild.

Current code:

- Reusable LangGraph subgraph exists with the required node names.
- Nodes are audited.
- Requirement uses payer rules and KG.
- Existing auth validation uses `auth_valid`.
- PA payload builder is invoked when PA is missing.
- Prior-auth request row is inserted.
- Valid existing auth patches `pre_auth_ref`.
- Missing PA produces `PA_REQUIRED_MISSING`.

Conformance: **Partial**

Remaining:

- `pa_route_decision` idempotency only scans in-memory repository internals when
  available; no repository method exists for pending-request lookup.
- `submit_or_create_task` always creates a local/manual task path; no live payer
  or OpenJet submit adapter.
- `poll_if_needed` and `parse_final_response` are stubs.
- WAITING_FOR_PAYER/callback integration is not called from this subgraph yet.
- Raw PA response S3 storage is not implemented in `parse_final_response`.
- Approved final PA response does not yet automatically trigger claim payload
  rebuild/new hash/new claim payload version.

## Section 9a - PA Claim Builder

MD requirement:

- PA source context reader.
- PA canonical builder.
- PA router.
- NPHIES PA builder with FHIR R4 Claim `use=preauthorization`.
- Shafafiya PA builder with DOH XML and OpenJet envelope.
- Validate PA payload against NPHIES profile or XSD.
- Store payload in S3 and PostgreSQL.

Current code:

- `PACanonicalForm` and canonical builder exist.
- PAClaimBuilderModule routes by `route.prior_auth_standard`.
- NPHIES PA builder emits FHIR JSON with `use=preauthorization`.
- Shafafiya PA builder emits XML.
- eClaimLink PA builder exists as a subclass placeholder.
- PA payload is hashed, stored in object store, and persisted in repository.

Conformance: **Partial**

Remaining:

- NPHIES PA bundle is minimal and lacks full MessageHeader/profile/resource
  conformance expected by a real NPHIES PA submission.
- Shafafiya PA builder does not include real OpenJet envelope details.
- PA builders do not call the payload validator before returning.
- eClaimLink PA builder is explicitly a placeholder.

## Section 10 - Status Enums

MD requirement:

- PayloadStatus, EligibilityStatus, PriorAuthStatus as listed.

Current code:

- All required enum values exist.
- Additional useful value exists: `PriorAuthStatus.REQUIRED_MISSING`.

Conformance: **Mostly implemented**

Remaining:

- If exact enum matching is mandatory, decide whether to keep or remove the
  extra `REQUIRED_MISSING` value.

## Section 11 - Idempotency

MD requirement:

- Every external touch uses idempotency key plus Redis `SET NX EX`.
- Callback idempotency is shared by webhook and poll.
- DB unique constraint protects callback double-processing.

Current code:

- Idempotency key helper exists.
- CallbackProcessor computes callback idempotency and uses `callback_lock`.
- Callback table has unique `idempotency_key`.
- Poll worker uses both `callback_lock` and `poll_lock`.

Conformance: **Partial**

Remaining:

- S3 writes, repository writes, FHIR reads/token calls, payer submits, and
  payload builds are not universally wrapped in the idempotency helper.
- Result-summary reuse for non-callback idempotency is not implemented.

## Section 12 - Polling, Background Jobs, Fallback/Callback

MD requirement:

- Enter WAITING_FOR_PAYER writes LangGraph checkpoint, Redis checkpoint key,
  bg job, callback state.
- Webhook receiver and poll worker resume safely.
- Locks: callback lock, poll lock, callback unique key.
- 24h escalation to NEEDS_REVIEW and `PAYER_RESPONSE_TIMEOUT`.

Current code:

- `enter_waiting_for_payer` writes checkpoint key, bg job, callback state, and
  `WAITING_FOR_PAYER`.
- `MemoryCheckpointStore` supports checkpoint storage and node-result injection.
- Webhook receiver exists and can build a FastAPI router.
- Poll worker finds due jobs, uses locks, processes final response, injects
  checkpoint result, archives callback, and escalates after 24h.
- CallbackProcessor stores callback_event and object-store callback payload.

Conformance: **Partial / production-shaped**

Remaining:

- The reusable eligibility/PA subgraphs do not currently call
  `enter_waiting_for_payer`.
- Checkpoint resume is injection plus `resume_callback`; it is not wired to a
  production LangGraph checkpointer/runtime.
- No long-running worker loop entrypoint/CLI is included; only worker functions.
- Escalation writes audit and returns error, but does not load and mutate the
  full canonical state from persistent storage.

## Section 13 - Error Taxonomy

MD requirement:

- Errors appended to `state.errors` follow `ClaimError`.
- CRITICAL stops, ERROR/WARNING score, INFO informational.

Current code:

- ClaimError exists.
- Checks emit CheckIssue.
- Validation converts CheckIssue to ClaimError and appends into canonical
  `errors` or `warnings`.
- Scoring handles CRITICAL, ERROR, WARNING, INFO.

Conformance: **Mostly implemented**

Remaining:

- Non-validation node exceptions are audited but not always converted into
  canonical `ClaimError` before graph failure.
- Error routing is strongest in validation, weaker in FHIR/context and
  preparation graphs.

## Section 14 - Audit Trail

MD requirement:

- Every node in every agent writes NODE_ENTER, NODE_EXIT, NODE_ERROR.
- Store audit in PostgreSQL and S3/object store.

Current code:

- `audited_node` wrapper exists.
- FHIR context agent nodes are audited.
- Claim preparation nodes are audited.
- Claim validation nodes are audited.
- Prior authorization agent node is audited.
- Eligibility subgraph nodes are audited.
- Prior-auth subgraph nodes are audited.
- Poll worker 24h escalation writes audit and object-store archive.

Conformance: **Mostly implemented**

Remaining:

- Audit is present for graph nodes; non-graph helper functions are not all
  audited individually.
- Audit payload snapshots are intentionally compact, not full input/output.

## Section 15 - Out of Scope

MD requirement:

- Submission Agent is future.
- Inter-agent handoff protocol is future.
- Current agents are called sequentially in-process.

Current code:

- No submission agent exists.
- Validation sets `next_agent = "SubmissionAgent"` only when ready to submit.
- Pipeline runs in-process.

Conformance: **Implemented**

## Section 16 - Production Readiness Checklist

MD checklist status against current code:

- PA Claim Builder: **Partial**
- Fallback/Callback: **Partial / production-shaped**
- Mock Neo4j: **Implemented**
- Mock payer rules: **Implemented**
- Live payer-rule circuit breaker: **Implemented, fetchers still manual**
- LLM coding enrichment: **Implemented as optional, non-authoritative**
- Eligibility TTL: **Implemented in rule model and local cache path**
- DAMAN VOI flag: **Implemented**
- Shafafiya OpenJet integration: **Partial / not real integration**
- Single route decision: **Implemented**
- Payload rebuild loop guard: **Implemented in validation**
- SHA256 payload hash: **Implemented on claim and PA payload builds**

## Highest-Priority Remaining Fixes

### P0 - Required For Real Workflow Behavior

1. Wire `enter_waiting_for_payer` into eligibility and PA subgraphs for queued
   or pended payer responses.
2. Add live eligibility and prior-auth payer/OpenJet/NPHIES adapters with submit
   and status polling.
3. Add repository methods for pending prior-auth request lookup and eligibility
   upsert; remove in-memory-only checks from the subgraphs.
4. Run official validation in claim prep or move `Validate_Claim` responsibility
   explicitly to validation stage.
5. Add ErrorHandler routes to FHIR context and claim preparation graphs.

### P1 - Required For Standards Readiness

1. Configure official Shafafiya and eClaimLink XSD paths and test against real
   schemas.
2. Configure official NPHIES FHIR validator command and IG assets.
3. Expand NPHIES PA builder to full message bundle profile.
4. Add real Shafafiya/OpenJet envelope for PA.
5. Replace eClaimLink PA placeholder with real schema-specific builder.

### P2 - Production Hardening

1. Apply idempotency guard to S3 writes, repository writes, FHIR calls, payer
   submits, and payload builds.
2. Add worker loop/CLI for polling jobs.
3. Add persistent checkpoint implementation backed by Redis/Postgres or the
   chosen LangGraph production checkpointer.
4. Add conformance tests for every MD section, not just the happy path.
5. Add RCM upload parsing for multipart, HL7, and CSV.

## Bottom Line

The project now matches the MD at the architectural level and passes local
pipeline tests. It is ready for local development and integration testing.

The remaining work is not spaghetti cleanup anymore; it is standards and
external-system integration work: real payer adapters, official schemas, real
OpenJet/NPHIES behavior, and stronger idempotent production orchestration.
