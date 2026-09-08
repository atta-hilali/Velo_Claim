# Pending DGX Migration

Status: `PENDING` - migration 006 must be applied on the legacy DGX database.

Before deploying the latest Shafafiya and controlled-submission changes on DGX,
apply every migration below that is not already recorded as applied:

`003_shafafiya_release_alignment.sql` supersedes the older
`003_prior_auth_display_id.sql`. Run the aligned migration below even if the
older display-ID migration was previously applied; its statements are
idempotent.

```bash
docker exec -i velo-claim-postgres \
  psql -U velo_claim -d velo_claim \
  --single-transaction \
  -v ON_ERROR_STOP=1 \
  < velo_claim/migrations/003_shafafiya_release_alignment.sql

docker exec -i velo-claim-postgres \
  psql -U velo_claim -d velo_claim \
  --single-transaction \
  -v ON_ERROR_STOP=1 \
  < velo_claim/migrations/004_prior_auth_response_optional_claim_link.sql

docker exec -i velo-claim-postgres \
  psql -U velo_claim -d velo_claim \
  --single-transaction \
  -v ON_ERROR_STOP=1 \
  < velo_claim/migrations/005_payer_submission_journal.sql

docker exec -i velo-claim-postgres \
  psql -U velo_claim -d velo_claim \
  --single-transaction \
  -v ON_ERROR_STOP=1 \
  < velo_claim/migrations/006_claim_intake_nullable_identifiers.sql
```

Verify it afterward:

```bash
docker exec velo-claim-postgres \
  psql -U velo_claim -d velo_claim \
  -c "SELECT column_name FROM information_schema.columns WHERE table_name = 'prior_auth_request' AND column_name = 'display_id';"
```

Also verify the durable submission journal:

```bash
docker exec velo-claim-postgres \
  psql -U velo_claim -d velo_claim \
  -c "SELECT to_regclass('public.payer_submission_journal');"
```

Expected results: one `display_id` row and `payer_submission_journal`.

After successful verification, change the status at the top of this file to
`APPLIED`, with the application date.
