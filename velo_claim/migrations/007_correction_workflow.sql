-- Durable human-reviewed correction workflow.
-- Apply after 001-006. This migration is additive and idempotent.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS correction_cycle (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    validation_report_id UUID NOT NULL REFERENCES validation_report(id) ON DELETE RESTRICT,
    base_claim_version INT NOT NULL,
    base_payload_version INT NOT NULL,
    cycle_number INT NOT NULL CHECK (cycle_number BETWEEN 1 AND 3),
    status TEXT NOT NULL CHECK (status IN (
        'NOT_STARTED', 'GENERATING', 'AWAITING_HUMAN_REVIEW',
        'PARTIALLY_REVIEWED', 'READY_TO_APPLY', 'APPLIED',
        'REJECTED', 'STALE', 'EXHAUSTED'
    )),
    payer_rule_source_version TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, validation_report_id, cycle_number)
);

CREATE TABLE IF NOT EXISTS correction_suggestion (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id UUID NOT NULL REFERENCES correction_cycle(id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    validation_report_id UUID NOT NULL REFERENCES validation_report(id) ON DELETE RESTRICT,
    base_claim_version INT NOT NULL,
    base_payload_version INT NOT NULL,
    issue_ids JSONB NOT NULL DEFAULT '[]',
    issue_codes JSONB NOT NULL DEFAULT '[]',
    field_path TEXT NOT NULL,
    old_value JSONB NOT NULL,
    proposed_value JSONB,
    source TEXT NOT NULL CHECK (source IN ('RULE_ENGINE', 'KG', 'LLM', 'MIXED', 'MANUAL_REQUIRED')),
    confidence NUMERIC(5, 4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    rationale TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    rule_refs JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN (
        'PENDING_REVIEW', 'MANUAL_RECONCILIATION_REQUIRED',
        'APPROVED', 'MODIFIED', 'REJECTED', 'STALE', 'APPLIED'
    )),
    cycle_count INT NOT NULL CHECK (cycle_count BETWEEN 1 AND 3),
    suggestion_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS correction_review (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suggestion_id UUID NOT NULL REFERENCES correction_suggestion(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'MODIFIED', 'REJECTED')),
    reviewer_id TEXT NOT NULL,
    modified_value JSONB,
    comment TEXT,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (suggestion_id)
);

CREATE TABLE IF NOT EXISTS correction_rule (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_key TEXT NOT NULL,
    version TEXT NOT NULL,
    issue_code TEXT NOT NULL,
    check_type TEXT NOT NULL,
    field_pattern TEXT NOT NULL,
    condition JSONB NOT NULL DEFAULT '{}',
    action JSONB NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    approved_by TEXT,
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'APPROVED', 'ACTIVE', 'RETIRED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_key, version)
);

ALTER TABLE claim_version
    ADD COLUMN IF NOT EXISTS correction_cycle_id UUID REFERENCES correction_cycle(id) ON DELETE SET NULL;

-- claim.current_version is the authoritative pointer. Historical claim_version
-- rows remain byte-for-byte immutable; correction application never rewrites
-- an older row merely to toggle its legacy is_current flag.

CREATE INDEX IF NOT EXISTS idx_correction_cycle_claim
    ON correction_cycle(claim_id, cycle_number DESC);
CREATE INDEX IF NOT EXISTS idx_correction_cycle_report
    ON correction_cycle(validation_report_id);
CREATE INDEX IF NOT EXISTS idx_correction_cycle_status
    ON correction_cycle(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_correction_suggestion_cycle
    ON correction_suggestion(cycle_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_correction_suggestion_claim_field
    ON correction_suggestion(claim_id, field_path, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_correction_suggestion_status
    ON correction_suggestion(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_correction_review_suggestion
    ON correction_review(suggestion_id, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_correction_rule_lookup
    ON correction_rule(issue_code, check_type, status);

DROP TRIGGER IF EXISTS trg_correction_cycle_updated_at ON correction_cycle;
CREATE TRIGGER trg_correction_cycle_updated_at BEFORE UPDATE ON correction_cycle
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_correction_suggestion_updated_at ON correction_suggestion;
CREATE TRIGGER trg_correction_suggestion_updated_at BEFORE UPDATE ON correction_suggestion
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_correction_rule_updated_at ON correction_rule;
CREATE TRIGGER trg_correction_rule_updated_at BEFORE UPDATE ON correction_rule
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
