-- Fresh 001 schemas did not include this column; older deployments did.
ALTER TABLE prior_auth_response
    ADD COLUMN IF NOT EXISTS claim_id TEXT REFERENCES claim(claim_id) ON DELETE SET NULL;
ALTER TABLE prior_auth_response
    ALTER COLUMN claim_id DROP NOT NULL;
