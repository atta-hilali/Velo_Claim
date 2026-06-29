# Velo Claim MD Conformance Audit

Source brief: `C:\Users\User\Downloads\velo_claim_codex_prompt_v2.md`

Audit date: 2026-06-29

## Executive Summary

The current `velo_claim/` package is clean, runnable, and much better organized
than the old prototype. It does **not** match every detail of the MD yet.

Current status:

- Architecture skeleton: **mostly present**
- Clean reusable modules: **present**
- Full production behavior from the MD: **partial**
- Exact conformance with every node/state/fallback/audit requirement: **not yet**

The biggest gaps are:

1. Eligibility and Prior Auth are reusable functions, not LangGraph sub-graphs.
2. Fallback/callback exists as primitives, but no webhook route, poll worker, or
   graph resume/checkpoint integration exists.
3. Audit trail is defined in storage but not written on every node entry/exit.
4. Real PostgreSQL/S3/Redis implementations are not present yet; only interfaces
   and in-memory development implementations exist.
5. Schema/profile validation for NPHIES, Shafafiya, and eClaimLink is only
   lightweight parsing, not full FHIR profile or XSD validation.
6. Payer-rule and FHIR vendor logic are preserved but not fully wired into the
   new package flow.

## Section-by-Section Conformance

## 0. Mental Model

MD requirement:

`Input Resolution -> Claim Preparation -> Claim Validation -> [FUTURE Submission]`,
with each stage as a LangGraph state machine, reusable sub-machines for
Eligibility and Prior Auth, and one canonical state object.

Current implementation:

- Full pipeline exists in `velo_claim/pipeline.py`.
- FHIR/context, claim prep, and validation are LangGraph agents:
  - `velo_claim/agents/fhir_context_agent.py:15`
  - `velo_claim/agents/claim_preparation.py:12`
  - `velo_claim/agents/claim_validation.py:15`
- Prior authorization is a plain function wrapper, not a LangGraph state
  machine: `velo_claim/agents/prior_authorization.py:9`.
- Eligibility and Prior Auth are functions, not reusable LangGraph sub-graphs:
  - `velo_claim/checks/eligibility.py:10`
  - `velo_claim/checks/prior_auth.py:13`

Conformance: **Partial**

Needed:

- Convert eligibility check into a reusable LangGraph subgraph.
- Convert prior-auth check into a reusable LangGraph subgraph.
- Decide whether `fhir_context_agent` is Agent 1 or an input-resolution module,
  because the MD labels it as Agent 1.

## 1. Technology Stack

MD requirement:

LangGraph, PostgreSQL, S3/MinIO, Redis, mock Neo4j, internal Redis-backed async
jobs, FHIR adapters, and NPHIES/Shafafiya/eClaimLink standards.

Current implementation:

- LangGraph is used in three agents.
- PostgreSQL schema exists: `velo_claim/migrations/001_initial_schema.sql`.
- Storage interfaces exist: `velo_claim/storage/interfaces.py:11`.
- In-memory repository/object/cache exist:
  - `velo_claim/storage/memory.py:18`
  - `velo_claim/storage/memory.py:128`
  - `velo_claim/storage/memory.py:144`
- Mock Neo4j exists:
  - `velo_claim/kg/interface.py:8`
  - `velo_claim/kg/mock.py:5`
- Mock payer rule loader exists: `velo_claim/rules/mock_loader.py:6`.
- FHIR vendor adapter logic is preserved:
  `velo_claim/context/vendor_fhir_adapters.py`.
- Claim standards builders exist:
  - `velo_claim/builders/claim/nphies.py`
  - `velo_claim/builders/claim/shafafiya.py`
  - `velo_claim/builders/claim/eclaimlink.py`

Conformance: **Partial**

Needed:

- Add real PostgreSQL repository implementation.
- Add real S3/MinIO object-store implementation.
- Add real Redis cache/lock implementation.
- Add Redis-backed background job worker.
- Wire vendor FHIR adapters into the new `AdapterInterface`.

## 2. Canonical State Object

MD requirement:

One JSON state object with identity, payload, PA payload, route, source context,
routing context, callback state, next agent, errors, and warnings. Errors and
warnings are append-only. Payload rebuild count must cap at 3.

Current implementation:

- Default state exists: `velo_claim/core/models.py:200`.
- Key fields are present:
  - `claim_payload`: `velo_claim/core/models.py:206`
  - `pa_payload`: `velo_claim/core/models.py:211`
  - `callback_state`: `velo_claim/core/models.py:217`
  - `errors/warnings`: `velo_claim/core/models.py:219`
- Error model exists: `velo_claim/core/models.py:19`.
- Append helpers exist: `velo_claim/core/models.py:192`.
- Validation checks use `CheckIssue`, not `ClaimError`, and do not append all
  issues into `state.errors`.
- `rebuild_attempt_count` exists but is only checked in validation:
  `velo_claim/agents/claim_validation.py:29`.

Conformance: **Partial**

Needed:

- Enforce append-only `state.errors` and `state.warnings` across all agents.
- Convert validation issues into state errors/warnings where appropriate.
- Make payload storage follow the MD rule consistently: raw payload only when
  small/local, object URI as source of truth.
- Enforce rebuild attempt cap in fallback flow, not only validation input.

## 3. Storage Architecture

MD requirement:

PostgreSQL tables, S3/MinIO object layout, Redis key patterns, mock Neo4j, mock
payer rules, and circuit-breaker pattern.

Current implementation:

- Initial PostgreSQL schema exists and includes the major tables:
  `velo_claim/migrations/001_initial_schema.sql`.
- `route_decision.claim_id` is unique:
  `velo_claim/migrations/001_initial_schema.sql:29`.
- `callback_event.idempotency_key` is unique:
  `velo_claim/migrations/001_initial_schema.sql:170`.
- In-memory repository has corresponding methods:
  `velo_claim/storage/memory.py:18`.
- In-memory object store exists: `velo_claim/storage/memory.py:128`.
- In-memory cache exists: `velo_claim/storage/memory.py:144`.
- Mock Neo4j interface and implementation match the MD shape:
  - `velo_claim/kg/interface.py:8`
  - `velo_claim/kg/mock.py:38`
- Mock payer loader exists: `velo_claim/rules/mock_loader.py:6`.

Conformance: **Partial**

Needed:

- Real PostgreSQL repository.
- Real object store.
- Real Redis implementation.
- Enforce the exact S3 key layout from the MD.
- Add `LivePayerRuleLoader` with circuit breaker and cached fallback.
- Add persistent `external_transaction` equivalent if desired; the MD uses
  `prior_auth_request`, `callback_event`, etc., but the architecture discussion
  also pointed to an external transaction manager.

## 4. FHIR Context Layer

MD requirement:

InputResolver supports RCM upload, Velo Doctor call, and encounter ID. FHIR
Adapter supports Epic, Cerner, Nabidh, TrakCare. FhirReaderTool dereferences
all references, fetches missing resources, normalizes documents. Routing and
clinical context extraction are separate responsibilities.

Current implementation:

- Input resolver exists: `velo_claim/context/input_resolver.py`.
- Adapter contract exists: `velo_claim/context/adapters.py:10`.
- In-memory FHIR adapter exists.
- Context resolver fetches missing patient/coverage/provider/facility and
  related resources when an adapter is provided:
  `velo_claim/context/fhir_context.py:18`.
- Routing context extraction exists:
  `velo_claim/context/fhir_context.py:91`.
- Old vendor adapters are preserved, but not wired into the new contract:
  `velo_claim/context/vendor_fhir_adapters.py`.

Conformance: **Partial**

Needed:

- Add RCM upload parsing for multipart/HL7/CSV.
- Add Velo Doctor input shape explicitly.
- Integrate `vendor_fhir_adapters.py` behind `AdapterInterface`.
- Add OAuth token cache via Redis key `token_cache:{provider_id}:{payer_id}`.
- Normalize documents into a stable internal document schema.

## 5. Router

MD requirement:

Router takes `routing_context`, resolves jurisdiction, standards, eligibility
profile, payer rule profile, submission channel, persists exactly one route
decision, and everyone downstream uses persisted route.

Current implementation:

- Router exists: `velo_claim/routing/router.py:9`.
- Route decision is persisted: `velo_claim/routing/router.py:34`.
- In-memory repository enforces one route unless identical:
  `velo_claim/storage/memory.py:40`.
- PostgreSQL migration has `route_decision.claim_id UNIQUE`:
  `velo_claim/migrations/001_initial_schema.sql:29`.
- Validation reloads persisted route:
  `velo_claim/agents/claim_validation.py:23`.

Conformance: **Mostly implemented**

Needed:

- Use `data/payers/phase1_payers.json` registry directly in route resolution.
- Make zero/multiple route checks explicit in validation. The DB uniqueness
  prevents multiple in production, but validation currently only checks missing.
- Add richer payer/platform profiles from the payer registry.

## 6. Claim Preparation State Machine

MD requirement:

`Start -> SupervisorNode -> PrepareClaimNode -> Validate_Claim -> End`, with an
ErrorHandler loop. Claim builder has source reader, SourceCodeExtractor,
canonical builder, format-specific builder. Validate_Claim performs structural
schema check.

Current implementation:

- Graph shape exists without ErrorHandler:
  `velo_claim/agents/claim_preparation.py:50`.
- Supervisor writes claim row:
  `velo_claim/agents/claim_preparation.py:19`.
- Claim builder exists:
  `velo_claim/builders/claim/builder.py`.
- Canonical claim builder exists:
  `velo_claim/builders/claim/canonical.py:13`.
- Bundling query is used during canonical build:
  `velo_claim/builders/claim/canonical.py:130`.
- Payload hash and object URI are stored:
  `velo_claim/builders/claim/builder.py:70`.
- Validate node only checks payload presence, not schema/XSD/FHIR profile:
  `velo_claim/agents/claim_preparation.py:41`.

Conformance: **Partial**

Needed:

- Add ErrorHandler route.
- Add structural schema/FHIR-profile/XSD validation.
- Split SourceCodeExtractor as its own reusable module/class if exact MD shape
  matters.
- Add audit events for every node entry/exit/error.

## 7. Claim Validation State Machine

MD requirement:

Graph nodes:

`normalize_validation_input -> load_route_and_context -> load_payer_rule ->
parse_payload -> ValidationNode -> CalculateScore -> Router -> End`, with
fallback loop.

Current implementation:

- Graph nodes mostly match:
  `velo_claim/agents/claim_validation.py:126`.
- `load_route_and_context` is a no-op:
  `velo_claim/agents/claim_validation.py:39`.
- Payer rules load exists:
  `velo_claim/agents/claim_validation.py:42`.
- Payload parsing once exists:
  `velo_claim/agents/claim_validation.py:57`.
- Validation orchestrator exists:
  `velo_claim/checks/orchestrator.py:19`.
- Score calculation exists:
  `velo_claim/checks/orchestrator.py:54`.
- No fallback edge/loop exists.
- No payload rebuild trigger back to Claim Preparation exists.
- Payer Rules Check is not a separate check module; payer rules are used inside
  prior-auth/documentation logic.

Conformance: **Partial**

Needed:

- Implement fallback node and loop.
- Implement payload rebuild handoff.
- Add dedicated payer rules check.
- Cache loaded rule set in Redis as the MD says.
- Add exact route-decision count check semantics.

## 8. Eligibility Check Sub-Graph

MD requirement:

Reusable LangGraph subgraph with normalize, cached eligibility, requirement
determination, platform routing, payload build, submit, poll, parse response,
validate details, patch claim, store record, finish.

Current implementation:

- Eligibility exists as one function:
  `velo_claim/checks/eligibility.py:10`.
- Cache support exists.
- Coverage active/period checks exist.
- DAMAN VOI check exists:
  `velo_claim/checks/eligibility.py:46`.

Conformance: **Low / Partial**

Needed:

- Convert to LangGraph subgraph.
- Add platform routing.
- Add eligibility payload builders.
- Add submit/poll/parse response path.
- Upsert `eligibility_check` records in repository.
- Patch claim with coverage/member/benefit details.

## 9. Prior Auth Check Sub-Graph

MD requirement:

Reusable LangGraph subgraph with normalize, requirement, existing auth,
idempotency route decision, PA builder, submit/task, poll, parse response,
patch claim, finish.

Current implementation:

- Prior-auth check exists as a function:
  `velo_claim/checks/prior_auth.py:13`.
- It determines PA requirement from rules/KG.
- It checks existing auth response validity.
- It builds PA payload when missing:
  `velo_claim/checks/prior_auth.py:57`.
- It inserts a prior auth request:
  `velo_claim/checks/prior_auth.py:62`.
- Prior-auth agent is not LangGraph:
  `velo_claim/agents/prior_authorization.py:9`.

Conformance: **Partial**

Needed:

- Convert to LangGraph subgraph.
- Add idempotency check for already submitted request.
- Add payer adapter submit/manual task creation.
- Add polling/waiting state integration.
- Add parse final response and raw response S3 storage.
- Add claim patch + payload rebuild workflow after approval.

## 9a. PA Claim Builder

MD requirement:

PA source reader, PA canonical form, PA router, NPHIES PA builder,
Shafafiya PA builder with OpenJet envelope, validation against profile/XSD,
store payload in S3 and PostgreSQL.

Current implementation:

- PA canonical form exists:
  `velo_claim/builders/prior_auth/canonical.py`.
- PA builder router exists:
  `velo_claim/builders/prior_auth/builder.py`.
- NPHIES PA builder exists:
  `velo_claim/builders/prior_auth/nphies.py`.
- Shafafiya PA builder exists:
  `velo_claim/builders/prior_auth/shafafiya.py`.
- eClaimLink PA placeholder exists:
  `velo_claim/builders/prior_auth/eclaimlink.py`.
- Payload URI/hash/version stored:
  `velo_claim/builders/prior_auth/builder.py`.

Conformance: **Partial**

Needed:

- Add OpenJet envelope for Shafafiya.
- Add FHIR profile validation for NPHIES PA.
- Add XSD validation for Shafafiya PA.
- Make eClaimLink PA real or explicitly out of scope.

## 10. Status Enums

MD requirement:

PayloadStatus, EligibilityStatus, PriorAuthStatus exactly as listed.

Current implementation:

- Enums exist:
  - `velo_claim/core/enums.py:18`
  - `velo_claim/core/enums.py:34`
  - `velo_claim/core/enums.py:43`
  - `velo_claim/core/enums.py:53`

Conformance: **Mostly implemented**

Note:

- `PriorAuthStatus.REQUIRED_MISSING` was added beyond the MD. It is useful, but
  not exact.

## 11. Idempotency

MD requirement:

Every external-system touch uses idempotency key + Redis `SET NX EX`; callbacks
have shared idempotency key and DB unique constraint.

Current implementation:

- Idempotency helper exists:
  `velo_claim/fallback/idempotency.py:19`.
- Callback DB uniqueness exists:
  `velo_claim/migrations/001_initial_schema.sql:170`.
- Callback processor uses lock and unique callback event:
  `velo_claim/fallback/callbacks.py:17`.

Conformance: **Partial**

Needed:

- Actually wrap external writes/calls in idempotency guard.
- Apply it to S3 writes, repository writes, FHIR calls, payer submit calls.
- Store/reuse result summaries as specified.

## 12. Polling, Background Jobs & Fallback/Callback

MD requirement:

Entering WAITING_FOR_PAYER persists checkpoint, bg job, callback state. Webhook
receiver and poll worker both resume LangGraph safely. Uses callback lock,
poll lock, callback event uniqueness. 24h escalation.

Current implementation:

- Waiting helper exists:
  `velo_claim/fallback/waiting.py:14`.
- Backoff schedule exists:
  `velo_claim/fallback/waiting.py:11`.
- Callback processor exists:
  `velo_claim/fallback/callbacks.py:10`.
- No webhook API route exists.
- No poll worker exists.
- No real LangGraph checkpoint persistence/resume exists.
- No `poll_lock` implementation exists.
- No 24h escalation implementation exists.

Conformance: **Low / Partial**

Needed:

- Add webhook endpoint module.
- Add background polling worker.
- Add checkpoint integration.
- Add poll lock.
- Add timeout escalation with `PAYER_RESPONSE_TIMEOUT`.

## 13. Error Taxonomy

MD requirement:

Every error appended to `state.errors` follows `ClaimError` schema. CRITICAL
stops, ERROR/WARNING score, INFO informational.

Current implementation:

- `ClaimError` exists:
  `velo_claim/core/models.py:19`.
- Checks currently return `CheckIssue`, not `ClaimError`.
- Validation report stores issues but does not append all errors into
  `state.errors`.

Conformance: **Partial**

Needed:

- Add central conversion from `CheckIssue` to `ClaimError`.
- Append errors/warnings to canonical state.
- Enforce routing semantics for CRITICAL/ERROR/WARNING/INFO across all agents.

## 14. Audit Trail

MD requirement:

Every node in every agent writes audit event on entry, exit, and error. Store in
PostgreSQL and S3.

Current implementation:

- Repository method exists:
  `velo_claim/storage/memory.py:103`.
- Migration table exists:
  `velo_claim/migrations/001_initial_schema.sql:151`.
- No agent node calls `insert_audit_event`.
- No S3 archival audit write exists.

Conformance: **Missing behavior / Storage only**

Needed:

- Add `audit_node` wrapper for LangGraph node functions.
- Write NODE_ENTER, NODE_EXIT, NODE_ERROR.
- Store audit event in repository and object store.

## 15. Out of Scope

MD requirement:

Submission Agent is future. Inter-agent handoff is future.

Current implementation:

- Submission agent is not implemented.
- Validation sets `next_agent = "SubmissionAgent"` only when ready:
  `velo_claim/agents/claim_validation.py:114`.

Conformance: **Implemented**

## 16. Production Readiness Checklist

Current checklist status:

- PA Claim Builder: **Partial**
- Fallback/Callback: **Partial**
- Neo4j mock: **Implemented**
- Payer rules mock: **Implemented**
- Live payer rule loader/circuit breaker: **Missing**
- LLM coding enrichment: **Missing**
- Eligibility TTL: **Implemented in function path**
- DAMAN VOI flag: **Implemented**
- Shafafiya OpenJet integration: **Missing**
- Single route decision: **Mostly implemented**
- Payload rebuild loop guard: **Partial**
- SHA256 payload hash: **Implemented on payload builds**

## Priority Fix List

### P0 - Required For MD Exactness

1. Add node audit wrapper and call it in all LangGraph agents.
2. Convert Eligibility Check to a reusable LangGraph subgraph.
3. Convert Prior Auth Check to a reusable LangGraph subgraph.
4. Implement fallback/rebuild loop in Claim Validation.
5. Implement callback/poll worker/checkpoint resume flow.
6. Add real schema validation for payloads.

### P1 - Required For Production Shape

1. Add PostgreSQL repository implementation.
2. Add S3/MinIO object store implementation.
3. Add Redis cache/lock implementation.
4. Wire vendor FHIR adapters into the new adapter interface.
5. Add live payer rule loader with circuit breaker.
6. Add dedicated payer rules check module.

### P2 - Useful Refinements

1. Add LLM-assisted coding enrichment behind a flag.
2. Add exact S3 object key layout tests.
3. Add conformance tests per MD section.
4. Add generated sample outputs from the new pipeline only.

## Bottom Line

The codebase now has the right **shape** and is cleanly organized. It is not yet
an exact implementation of the MD. The next pass should focus on behavior that
the MD explicitly requires but the package currently only represents as
interfaces or simplified functions: subgraphs, audit trail, callback/polling,
fallback rebuild, real storage adapters, and schema validation.

## Implementation Update - 2026-06-29

This follow-up pass closed the specific audit gaps called out after the clean
rebuild:

- Eligibility and Prior Auth are now reusable LangGraph subgraphs:
  - `velo_claim/checks/eligibility_graph.py`
  - `velo_claim/checks/prior_auth_graph.py`
- The dedicated Prior Authorization agent now wraps the reusable PA subgraph:
  - `velo_claim/agents/prior_authorization.py`
- Node audit is now applied to top-level agents and reusable subgraph nodes.
  Audit events write `NODE_ENTER`, `NODE_EXIT`, and `NODE_ERROR` to the
  repository and object store when available:
  - `velo_claim/agents/audit.py`
- PostgreSQL, S3/MinIO, and Redis production adapters now exist behind the same
  interfaces as the in-memory dev adapters:
  - `velo_claim/storage/postgres.py`
  - `velo_claim/storage/object_store.py`
  - `velo_claim/storage/redis_cache.py`
- Fallback/callback now includes:
  - webhook receiver/router helper: `velo_claim/api/webhooks.py`
  - poll worker with `callback_lock`, `poll_lock`, checkpoint injection, and
    24h escalation: `velo_claim/jobs/poll_worker.py`
  - local checkpoint store: `velo_claim/fallback/checkpoints.py`
  - WAITING_FOR_PAYER helper that writes `lg_checkpoint:*`, `bg_job:*`, and
    callback state: `velo_claim/fallback/waiting.py`
- Payload validation is standard-aware:
  - NPHIES local FHIR Bundle/Claim checks plus optional external FHIR validator
    command via `NPHIES_FHIR_VALIDATOR_COMMAND`
  - Shafafiya/eClaimLink XML parse checks plus XSD validation when
    `SHAFAFIYA_CLAIM_XSD_PATH` or `ECLAIMLINK_CLAIM_XSD_PATH` is configured
  - `velo_claim/validation/payload_validators.py`
- Vendor FHIR adapters are bridged into the new `AdapterInterface`, auto-wired
  when FHIR env vars are present, and OAuth tokens can be cached in Redis under
  `token_cache:{provider_id}:{payer_id}`:
  - `velo_claim/context/vendor_bridge.py`
  - `velo_claim/agents/fhir_context_agent.py`
- The phase-1 payer registry is now used by the router for jurisdiction and
  standard inference:
  - `velo_claim/routing/router.py`
  - `velo_claim/routing/payer_registry.py`
- A dedicated payer-rules validation module now exists:
  - `velo_claim/checks/payer_rules.py`
- Payer rule loading now has a production-shaped live/cached/mock circuit
  breaker:
  - `velo_claim/rules/live_loader.py`
- Coding consistency now supports optional LLM enrichment behind environment
  flags:
  - `velo_claim/checks/coding.py`
- Validation now explicitly checks that exactly one route decision exists and
  appends validation findings back into canonical state `errors`/`warnings`.

Remaining infrastructure-dependent items:

- Real payer portal fetchers are still not implemented; `LivePayerRuleLoader`
  accepts an injected fetcher and falls back to cached/mock data.
- Full NPHIES FHIR profile validation requires an external configured validator
  command and IG assets.
- Full Shafafiya/eClaimLink validation requires the official XSD paths to be
  configured.
- Webhook/poll resume supports checkpoint injection plus a `resume_callback`
  hook; production deployment still needs to wire that hook to the chosen
  LangGraph runtime/checkpointer.
