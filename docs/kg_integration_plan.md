# Velo Claim — KG integration plan and resume notes

Recorded: 7 September 2026.

## User setup and current pause

- The project exists on both the user's local PC and the DGX.
- The user normally edits code locally and uses the DGX for Docker and heavier workloads.
- The DGX is currently down. Do not make DGX access a prerequisite for ordinary local coding work.
- KG runtime inspection, import, and integration verification are paused until the DGX is available. No Docker start, import, database mutation, or KG code integration has been performed in this conversation.
- This document records the next steps; it does not schedule or authorize unattended future execution.

## What the user already has

The user reports a prebuilt KG with substantial ICD, CDT, and payer-rule information. Its actual schema, coverage, source quality, and runtime location have not yet been verified.

The GitHub tree contains `import/kg.cypher` (28,758,705 bytes), a likely candidate. The connected GitHub reader could not retrieve its contents because of its size. Do not claim its labels, relationships, import format details, or completeness are known. Confirm whether this is the user's intended graph.

## Findings from the reviewed repository

Reviewed repository: https://github.com/atta-hilali/Velo_Claim

Reviewed main snapshot: `bcf8565becb00b4dfa6094517d867b379a9d787d`. Local/DGX unpushed changes were not inspected.

> Implementation update (2026-09-08): the DGX graph was inspected read-only
> and is already populated. The runtime integration described below is now
> implemented with the official Neo4j driver, explicit backend selection,
> evidence-bearing results, code-system-aware queries, and health diagnostics.
> See `docs/kg_runtime_schema.md` for the observed production graph contract.

- `velo_claim/core/container.py` selects `MockNeo4jClient` in both memory and production storage modes.
- Production storage mode selects PostgreSQL, S3/MinIO, and Redis, but this does not activate a real graph connection.
- `LivePayerRuleLoader` is constructed with `fetcher=None`. It can read persisted/cached rules and otherwise falls back to mock defaults; it cannot fetch live rules in that configuration.
- `velo_claim/kg/interface.py` exposes compatibility, prior-authorization requirement, bundling, and documentation queries. Its names and parameters are oriented toward CPT.
- `velo_claim/rules/mock_loader.py` supplies small hardcoded payer/plan examples.
- A KG-backed rule loader can replace those examples without a payer-portal fetcher if the graph is an appropriate rule source. Live portal integration is a separate capability.
- `import/cdt_cost.json` also exists; its currency, provenance, effective dates, and intended use have not been established. Do not treat its numbers as authoritative payer fees.

## Resume sequence

### 1. Identify the authoritative checkout and graph

- Check the branch, commit, and working-tree changes on local and DGX before syncing. Preserve uncommitted work; do not blindly overwrite either checkout.
- Confirm the actual prebuilt graph file or existing database and whether `import/kg.cypher` is the correct artifact.
- Inspect the export's beginning, constraints, labels, relationship types, and any executable import directives before running it.
- Determine the Neo4j version required by the export and the actual running version.

### 2. Inspect the existing DGX service before starting or importing anything

From the project directory on the DGX, begin with:

```bash
docker compose ps neo4j
```

The reviewed Compose configuration defines service `neo4j`, image `neo4j:5.12-community`, host port `7475` to container `7474`, and host port `7688` to container `7687`. It mounts `./neo4j_data` at `/data` and `./import` at `/import`. Confirm these still match the actual deployment.

Mounting the import directory does not execute the graph export. Conversely, the persistent data directory may already contain a loaded graph. Check first to avoid duplicate imports.

If the service is stopped, inspect its logs/configuration and start only the intended Neo4j service when appropriate.

### 3. Open Neo4j from the local PC

Use the user's working DGX SSH host and credentials. Forward both the Browser and Bolt host ports; use placeholders until the current SSH address is confirmed:

```bash
ssh -N -L 7475:127.0.0.1:7475 -L 7688:127.0.0.1:7688 USER@DGX_HOST
```

Keep the tunnel open. Open `http://localhost:7475` on the PC and connect with `bolt://localhost:7688`, using the existing Neo4j credentials. Do not ask the user to paste passwords into chat. If local ports are occupied, choose alternative local ports consistently.

Backend connection choices, subject to the verified deployment:

| Backend location | Neo4j URI |
| --- | --- |
| Local PC through the tunnel | `bolt://localhost:7688` |
| DGX host | `bolt://localhost:7688` |
| Backend container on the same Compose network | `bolt://neo4j:7687` |

### 4. Check whether data already exists

Run read-only queries in the intended Neo4j database:

```cypher
MATCH (n) RETURN count(n) AS node_count;
```

```cypher
CALL db.labels();
```

```cypher
CALL db.relationshipTypes();
```

```cypher
MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 25;
```

If the database appears empty, confirm the selected database and mounted data directory before importing. If it is populated, inspect and reuse it where suitable. Before any import into an existing database, preserve a recoverable backup or use an isolated test instance. Select the import method only after inspecting the file and version compatibility; do not assume re-running the export is idempotent.

### 5. Map the graph schema to Velo Claim

Document actual node labels, properties, relationships, identifier formats, code-system versions, payer aliases, and plan identifiers. Establish whether the graph contains only code catalogs or also the relationships needed for validation.

Required query capabilities:

| Capability | Information to establish |
| --- | --- |
| Diagnosis/procedure compatibility | ICD system/version, CDT or CPT system/version, code pair, supporting relationship and evidence |
| Prior authorization | Payer, plan, procedure, service date, conditions and exceptions |
| Bundling | Procedure combination and applicable payer/plan restrictions |
| Required documentation | Procedure/context, document type, applicable rule |
| Rule applicability | Jurisdiction, payer, plan, effective dates, source, version, approval status where available |

Treat ICD, CDT, and CPT as distinct code systems. Never invent relationship names or assume CDT and CPT are interchangeable. Flag missing evidence or coverage for review.

### 6. Implement the real graph client locally

- Add a real implementation of the graph interface using the Neo4j Python driver and parameterized read queries.
- Configure URI, database, and credentials through environment settings; verify connectivity explicitly.
- Adapt the CPT-oriented interface and callers to carry the procedure code system, including CDT.
- Return explicit supported/unsupported/unknown results with source evidence where possible. A missing edge is not proof of incompatibility; a missing PA rule is not proof that authorization is unnecessary.
- Handle unavailable databases and unsupported rule conditions explicitly rather than silently converting them to passing checks.

### 7. Implement a KG-backed payer-rule loader

- Read relevant graph rules and adapt them to the validation engine's contract.
- Extend the current `PayerRuleSet` if needed: it currently lacks a dedicated KG source value and detailed rule provenance/effective-date fields.
- Preserve payer/plan matching, source, version, and rule applicability. Clearly distinguish cached rule snapshots from newly retrieved ones.
- Decide a single authority for overlapping KG and payer-rule checks to prevent inconsistent or duplicated decisions.
- Keep a live payer-portal fetcher separate; it is not required to connect this prebuilt KG.

### 8. Wire configuration and evidence into the application

- Update the service container to select the real graph client and the intended rule loader explicitly, independently of the storage mode where useful.
- Keep mock mode available for tests but prevent an enabled real integration from silently falling back to hardcoded data.
- Report graph connectivity and rule source clearly in service diagnostics and validation evidence.
- Route unknown or unavailable required knowledge to review. Resolve the existing readiness logic that can allow a general ERROR at score 80 to become `READY_TO_SUBMIT` where it undermines these gates.

### 9. Verify on the DGX when available

- Use a small, inspected set of synthetic claims and known graph facts.
- Cover an ICD–CDT pair, an ICD–CPT pair if supported, payer-specific authorization, documentation, and bundling.
- Cover unknown codes/payers, absent relationships, expired or conflicting rules, and database unavailability.
- Compare the graph's expected answers with the validation report, including provenance and review outcomes.
- Verify that graph-backed decisions are actually returned, not results from the mock classes.
- Run the existing pipeline tests and focused integration tests needed for the changed behavior.

## Completion criteria

- The user can open and inspect the actual KG from the local PC.
- The graph is loaded or confirmed already loaded without losing existing data or duplicating an import.
- Velo Claim queries the real graph and retrieves applicable payer rules.
- ICD/CDT/CPT identities are preserved throughout relevant checks.
- Missing evidence and unavailable data produce an explicit review outcome.
- Validation reports identify the source and rule evidence used.
- Local code changes and DGX integration verification are recorded separately.

## First action when resuming

Confirm DGX availability, identify the authoritative project checkout, then obtain `docker compose ps neo4j` output. Open the existing database and inspect its schema before deciding whether an import or implementation change is needed.

## Reference sources

- Container wiring: https://github.com/atta-hilali/Velo_Claim/blob/bcf8565becb00b4dfa6094517d867b379a9d787d/velo_claim/core/container.py
- Graph interface: https://github.com/atta-hilali/Velo_Claim/blob/bcf8565becb00b4dfa6094517d867b379a9d787d/velo_claim/kg/interface.py
- Docker configuration: https://github.com/atta-hilali/Velo_Claim/blob/bcf8565becb00b4dfa6094517d867b379a9d787d/docker-compose.yml
- Neo4j Python connectivity: https://neo4j.com/docs/python-manual/current/connect/
- Neo4j Docker operations: https://neo4j.com/docs/operations-manual/current/docker/operations/
