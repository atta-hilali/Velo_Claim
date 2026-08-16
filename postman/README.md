# Velo Claim Postman Tests

## Import

1. Start the DGX tunnel and frontend with `./start_velo.ps1` from PowerShell.
2. In Postman, select **Import**.
3. Import `Velo Claim API.postman_collection.json`.
4. Import `Velo Claim - Local Tunnel.postman_environment.json`.
5. Select **Velo Claim - Local Tunnel** from the environment menu.

## Run

Run **01 - Health** first. It must return HTTP 200 with `status: ok`.

Use the collection runner to execute the requests in numeric order. Request 02
creates a unique `claim_id` and stores it as a collection variable. Every later
request uses that generated ID automatically.

The collection tests:

- API health
- encounter-to-claim pipeline
- claims queue retrieval
- claim detail retrieval
- raw XML/FHIR payload retrieval
- status updates and escalation audit
- payer callback ingestion

## Important limits

- `approve_submit` is intentionally not part of the automated run because the
  current endpoint changes database status without transmitting to a payer.
- The callback request tests ingestion and persistence, not durable automatic
  LangGraph resumption.
- The current API has no authentication or webhook signature verification. Use
  it only through the localhost SSH tunnel until those controls are implemented.
- The `/health` response identifies configured adapters but does not actively
  test PostgreSQL, Redis, MinIO, FHIR, or payer connectivity.

## Troubleshooting

If Postman returns `ECONNREFUSED 127.0.0.1:8000`, restart `start_velo.ps1` and
confirm `http://127.0.0.1:8000/health` opens in the browser.

If request 02 returns HTTP 500, inspect the DGX API logs:

```bash
docker logs --tail 200 velo-claim-api
```

If request 03 cannot find the newly created claim, verify that the API health
response reports `PostgresRepository` rather than `InMemoryRepository`.
