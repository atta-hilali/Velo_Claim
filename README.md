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
- `velo_claim/ingestion/` validates encounter PDFs, extracts a pipeline-shaped
  encounter package, and blocks incomplete routing data before claim creation.
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
- `velo_claim/kg/` provides a read-only Neo4j client with explicit JSON and
  mock test backends. `velo_claim/rules/` combines versioned payer rules with
  graph evidence without treating missing graph facts as negative decisions.
- `velo_claim/agents/` contains thin LangGraph state machines.
- `velo_claim/corrections/` and
  `velo_claim/agents/correction_suggester.py` implement the reusable,
  human-reviewed correction workflow for `NEEDS_REVIEW` claims.
- `velo_claim/security/generate_jwks.py` preserves JWKS generation without
  exposing private keys.
- `velo_claim/migrations/` contains the ordered PostgreSQL migrations. Apply
  every unapplied migration through `007_correction_workflow.sql`.
- `data/schemas/shafafiya/v2.0/` contains the immutable official XSD release
  used for claims, eligibility, prior authorization, and remittance responses.

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
python -m pytest -q
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

## Test Doubles and Production Services

Automated tests can inject isolated in-memory implementations:

```text
InMemoryRepository  -> PostgreSQL-shaped records
InMemoryObjectStore -> S3/MinIO-shaped payload storage
InMemoryCacheStore  -> Redis-shaped cache/locks
MockNeo4jClient     -> explicit test-only graph responses
MockPayerRuleLoader -> explicit test-only payer/plan rules
```

Production selects the graph backend explicitly with
`VALIDATION_KG_BACKEND=neo4j`. It uses `NEO4J_URI`, `NEO4J_USER`,
`NEO4J_PASSWORD`, and `NEO4J_DATABASE`; it does not silently fall back to mock
knowledge if Neo4j is unavailable. The `/health` response exposes graph
connectivity and reports a degraded service when the graph cannot be reached.

The correction workflow also uses the DGX MedGemma service through
`CORRECTION_LLM_*` configuration, with fallback to `VALIDATION_LLM_*` and
`MEDGEMMA_*`. Production container construction rejects a mock KG. See
[Correction Suggester and Human Review](docs/correction_suggester.md) for the
graph, reviewer API, migration, safety rules, and deployment configuration.

Production replacements should implement the same interfaces, not change the
agent code.

## Controlled Shafafiya Test Submission

Claim and PA submission now use exact-payload human approval, an auditable
submission journal, a WSDL-bound Shafafiya adapter, and response reconciliation.
Delivery acknowledgement is separate from the payer decision. The default is
**disabled**; manual test-portal exchange and PTE are supported. Production
still requires authoritative payer-rule feeds and configured payer endpoints;
the populated Neo4j graph can now be selected as the validation KG backend.

See [setup, API workflow, and verification](docs/shafafiya_submission.md) and the
[KG integration plan](docs/kg_integration_plan.md). Run
`python -m pytest -q` for the local checks and `npm run build` in `frontend/`.

## Manual Encounter PDF Import

The RCM queue includes **Import encounter**. It uploads one searchable PDF to
`POST /encounters/pdf`, stores the original document in S3/MinIO, extracts the
encounter context, and starts the existing context, routing, preparation, and
validation pipeline. Re-uploading the exact same PDF returns the existing
claim instead of creating a duplicate.

The backend accepts PDFs up to 10 MB and 100 pages by default. A PDF without a
usable text layer is rejected with `PDF_OCR_REQUIRED`; configure
`PDF_OCR_ENDPOINT` for scanned-document OCR. Optional MedGemma extraction is
disabled by default and can be enabled with `PDF_ENCOUNTER_USE_LLM=true` after
configuring an internal extraction endpoint. The deterministic parser combines
normal page text, layout-preserved text, AcroForm values, label/value blocks,
claim-form tables, code-system suffixes, and charge-summary rows. This supports
different searchable templates without relying on fixed PDF coordinates. Missing
patient, payer, service date, or facility facts stop the pipeline and are returned
to the RCM user instead of being inferred.

After changing `requirements.txt`, rebuild the DGX API image:

```bash
cd /home/dev1/Desktop/data/features/feature_atta/Velo_claim
bash deploy/docker/deploy_api.sh
```
