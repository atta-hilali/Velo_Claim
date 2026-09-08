-- Complete the PA runtime columns required by the Shafafiya transaction flow.
-- Additive and safe to run repeatedly after 001 and 002.

ALTER TABLE prior_auth_request
    ADD COLUMN IF NOT EXISTS display_id TEXT;

-- Reusable PA requests may be prepared before a Velo Claim record exists.
ALTER TABLE prior_auth_request
    ALTER COLUMN claim_id DROP NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'prior_auth_response'
          AND column_name = 'claim_id'
    ) THEN
        ALTER TABLE prior_auth_response ALTER COLUMN claim_id DROP NOT NULL;
    END IF;
END
$$;

UPDATE prior_auth_request
SET display_id = 'PA-' || upper(substr(replace(id::text, '-', ''), 1, 12))
WHERE display_id IS NULL OR display_id = '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_prior_auth_request_display_id
    ON prior_auth_request(display_id);

COMMENT ON COLUMN prior_auth_request.display_id IS
    'Human-readable Velo Claim identifier; the payer Authorization.ID remains in the XML payload.';
