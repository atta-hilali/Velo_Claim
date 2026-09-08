-- Apply after the existing migrations. Stores exact approvals, outbound
-- attempts and response progress independently of UI lifecycle status.
CREATE TABLE IF NOT EXISTS payer_submission_journal (
    key TEXT PRIMARY KEY,
    document JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payer_submission_journal_type
    ON payer_submission_journal ((document->>'record_type'));

-- Align databases created from either historical schema variant.
ALTER TABLE prior_auth_response
    ADD COLUMN IF NOT EXISTS claim_id TEXT REFERENCES claim(claim_id) ON DELETE SET NULL;
ALTER TABLE prior_auth_response ALTER COLUMN claim_id DROP NOT NULL;
