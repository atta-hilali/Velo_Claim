-- Align databases created from the legacy Velo Claim schema with the
-- eligibility/prior-authorization runtime used by the current application.
-- This migration is additive and is safe to run more than once.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TYPE prior_auth_status_enum ADD VALUE IF NOT EXISTS 'REQUIRED_MISSING';
ALTER TYPE prior_auth_status_enum ADD VALUE IF NOT EXISTS 'MANUAL_PORTAL_TASK';

CREATE TABLE IF NOT EXISTS eligibility_request (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    standard TEXT NOT NULL DEFAULT 'MANUAL',
    payload_type TEXT,
    object_uri TEXT,
    sha256_hash TEXT,
    patient_id TEXT,
    payer_id TEXT,
    plan_id TEXT,
    coverage_id TEXT,
    member_id TEXT,
    service_date DATE,
    status TEXT NOT NULL DEFAULT 'CREATED',
    external_transaction_id TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}',
    callback_state JSONB NOT NULL DEFAULT '{}',
    submitted_at TIMESTAMPTZ,
    waiting_since TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ,
    poll_attempt INT NOT NULL DEFAULT 0,
    response_due_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE eligibility_check ADD COLUMN IF NOT EXISTS request_id UUID;
ALTER TABLE eligibility_check ADD COLUMN IF NOT EXISTS plan_id TEXT;
ALTER TABLE eligibility_check ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE eligibility_check ADD COLUMN IF NOT EXISTS eligibility_ref TEXT;
ALTER TABLE eligibility_check ADD COLUMN IF NOT EXISTS voi_ref TEXT;
ALTER TABLE eligibility_check ADD COLUMN IF NOT EXISTS payer_response JSONB NOT NULL DEFAULT '{}';
ALTER TABLE eligibility_check ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS pa_payload (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    version INT NOT NULL,
    standard TEXT NOT NULL,
    payload_type TEXT NOT NULL,
    object_uri TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,
    required_codes JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, version)
);

ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS pa_payload_id UUID;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS payload_type TEXT;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS sha256_hash TEXT;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS required_codes JSONB NOT NULL DEFAULT '[]';
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS payer_id TEXT;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS plan_id TEXT;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS service_date DATE;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS external_transaction_id TEXT;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS portal_task_id TEXT;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS request_payload JSONB NOT NULL DEFAULT '{}';
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS callback_state JSONB NOT NULL DEFAULT '{}';
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS waiting_since TIMESTAMPTZ;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS next_poll_at TIMESTAMPTZ;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS poll_attempt INT NOT NULL DEFAULT 0;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS response_due_at TIMESTAMPTZ;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE prior_auth_request ADD COLUMN IF NOT EXISTS error_message TEXT;

ALTER TABLE prior_auth_response ADD COLUMN IF NOT EXISTS normalized_response JSONB NOT NULL DEFAULT '{}';
ALTER TABLE prior_auth_response ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE prior_auth_response ADD COLUMN IF NOT EXISTS external_transaction_id TEXT;
ALTER TABLE prior_auth_response ADD COLUMN IF NOT EXISTS cpt_codes JSONB NOT NULL DEFAULT '[]';
ALTER TABLE prior_auth_response ADD COLUMN IF NOT EXISTS valid_from DATE;
ALTER TABLE prior_auth_response ADD COLUMN IF NOT EXISTS valid_to DATE;
ALTER TABLE prior_auth_response ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS callback_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    job_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('WEBHOOK', 'POLL', 'MANUAL')),
    event_type TEXT,
    transaction_ref TEXT,
    idempotency_key TEXT UNIQUE NOT NULL,
    raw_payload JSONB NOT NULL,
    normalized_payload JSONB NOT NULL DEFAULT '{}',
    raw_payload_uri TEXT,
    status TEXT NOT NULL DEFAULT 'RECEIVED',
    processed_at TIMESTAMPTZ,
    error_message TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eligibility_request_claim
    ON eligibility_request(claim_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eligibility_check_claim_latest
    ON eligibility_check(claim_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_pa_payload_claim_latest
    ON pa_payload(claim_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_prior_auth_request_claim
    ON prior_auth_request(claim_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_callback_claim_received
    ON callback_event(claim_id, received_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'eligibility_check_request_id_fkey'
    ) THEN
        ALTER TABLE eligibility_check
            ADD CONSTRAINT eligibility_check_request_id_fkey
            FOREIGN KEY (request_id) REFERENCES eligibility_request(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'prior_auth_request_pa_payload_id_fkey'
    ) THEN
        ALTER TABLE prior_auth_request
            ADD CONSTRAINT prior_auth_request_pa_payload_id_fkey
            FOREIGN KEY (pa_payload_id) REFERENCES pa_payload(id) ON DELETE SET NULL;
    END IF;
END
$$;
