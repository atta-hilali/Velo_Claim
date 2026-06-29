# Velo Claim

Velo Claim is a clean, package-based medical-claim automation prototype for
UAE/KSA claim preparation, validation, prior authorization, and future
submission workflows.

The active implementation lives in `velo_claim/`. The old monolithic prototype
scripts have been removed; reusable business capabilities now live in modules,
and LangGraph agents only orchestrate those modules.

## Active Package Layout

- `velo_claim/context/` resolves raw encounter/FHIR input into `source_context`
  and `routing_context`.
- `velo_claim/context/vendor_fhir_adapters.py` preserves the previous Epic,
  TrakCare/IRIS, Oracle Health/Cerner, NABIDH, and generic FHIR connection
  logic for future integration into the new context layer.
- `velo_claim/routing/` persists exactly one route decision per claim.
- `velo_claim/routing/payer_registry.py` preserves the Phase 1 payer registry
  helper logic.
- `velo_claim/builders/claim/` builds canonical claims and serializes to
  NPHIES, Shafafiya, or eClaimLink payloads.
- `velo_claim/builders/prior_auth/` builds reusable prior-authorization
  payloads.
- `velo_claim/checks/` contains reusable validation checks: metadata, payload
  conformity, financial consistency, eligibility, prior auth, coding,
  documentation, duplicate, and readiness.
- `velo_claim/fallback/` contains idempotency, callback, and waiting-for-payer
  primitives.
- `velo_claim/storage/` defines PostgreSQL/S3/Redis-shaped interfaces with
  in-memory implementations for local development.
- `velo_claim/kg/` and `velo_claim/rules/` provide mock Neo4j and mock payer
  rule loaders behind production-ready interfaces.
- `velo_claim/agents/` contains thin LangGraph state machines.
- `velo_claim/security/generate_jwks.py` preserves JWKS generation without
  exposing private keys.
- `velo_claim/migrations/001_initial_schema.sql` contains the first PostgreSQL
  schema migration.

## Preserved Data And Connection Artifacts

These are intentionally kept:

- `.env` and `.env.example`
- `keys/` private key directory, ignored by git
- `public/nonprod/jwks.json` and `public/prod/jwks.json`
- `data/coding_knowledge_graph.json`
- `data/payer_rules/default_rules.json`
- `data/payers/phase1_payers.json`
- `data/prior_auth_extraction/velo_claim_prior_auth_extraction_register.json`
- `sample_inputs/` encounter and claim fixtures

Generated `sample_outputs/` files were removed because they can be recreated
from tests and should not be the source of truth.

## Run The Clean Pipeline Test

```powershell
python -m pytest test_clean_rebuild_pipeline.py -q
```

The test runs the current package end to end:

```text
FHIR/context resolution
-> route decision
-> claim preparation
-> Shafafiya payload build
-> validation checks
-> final READY_TO_SUBMIT decision
```

## Local Default Stack

The local default container uses runnable development implementations:

```text
InMemoryRepository  -> PostgreSQL-shaped records
InMemoryObjectStore -> S3/MinIO-shaped payload storage
InMemoryCacheStore  -> Redis-shaped cache/locks
MockNeo4jClient     -> coding and prior-auth graph queries
MockPayerRuleLoader -> payer/plan rules
```

Production replacements should implement the same interfaces, not change the
agent code.
