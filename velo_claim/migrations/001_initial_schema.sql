-- Velo Claim initial PostgreSQL schema
--
-- Design goals:
--   1. Keep the current repository methods working without code changes.
--   2. Store the built claim payload as the core artifact of the workflow.
--   3. Preserve enough relational columns for queues, lookup, polling, dedupe,
--      and reporting, while storing rich FHIR / payer data as JSONB.
--   4. Make async payer flows durable: eligibility, prior auth, callbacks,
--      background poll jobs, checkpoints, and idempotency.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Shared helpers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Core claim identity and workflow state
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS claim (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT UNIQUE NOT NULL,

    status TEXT NOT NULL,
    lifecycle_status TEXT,
    jurisdiction TEXT,
    claim_standard TEXT,

    payer_id TEXT,
    payer_name TEXT,
    plan_id TEXT,
    coverage_id TEXT,
    member_id TEXT,

    patient_id TEXT,
    patient_name TEXT,
    provider_id TEXT,
    facility_id TEXT,
    encounter_id TEXT,
    source_system TEXT,

    service_date DATE,
    currency TEXT,
    total_amount NUMERIC(14, 2),

    current_version INT NOT NULL DEFAULT 0,
    current_payload_version INT NOT NULL DEFAULT 0,
    current_pa_payload_version INT NOT NULL DEFAULT 0,
    rebuild_attempt_count INT NOT NULL DEFAULT 0,

    pre_auth_ref TEXT,
    eligibility_ref TEXT,
    callback_state JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The FHIR context stage is intentionally separated from claim_version.
-- It records how we got the context and stores the complete extraction result
-- for replay/debugging without requiring another EHR/FHIR call.
CREATE TABLE IF NOT EXISTS fhir_context_session (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT REFERENCES claim(claim_id) ON DELETE SET NULL,
    trigger_source TEXT,
    source_system TEXT,
    adapter_name TEXT,
    source_endpoint TEXT,
    source_encounter_id TEXT,
    status TEXT NOT NULL DEFAULT 'STARTED',
    routing_context JSONB NOT NULL DEFAULT '{}',
    source_context JSONB NOT NULL DEFAULT '{}',
    request_metadata JSONB NOT NULL DEFAULT '{}',
    warnings JSONB NOT NULL DEFAULT '[]',
    errors JSONB NOT NULL DEFAULT '[]',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fhir_resource_snapshot (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES fhir_context_session(id) ON DELETE CASCADE,
    claim_id TEXT REFERENCES claim(claim_id) ON DELETE SET NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    full_url TEXT,
    source_endpoint TEXT,
    resource_json JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, resource_type, resource_id, content_hash)
);

CREATE TABLE IF NOT EXISTS route_decision (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT UNIQUE NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    jurisdiction TEXT,
    claim_standard TEXT,
    prior_auth_standard TEXT,
    eligibility_profile TEXT,
    payer_rule_profile TEXT,
    submission_channel TEXT,
    confidence NUMERIC(5, 4),
    routing_context JSONB NOT NULL DEFAULT '{}',
    route JSONB NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    warnings JSONB NOT NULL DEFAULT '[]',
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claim_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    version INT NOT NULL,
    parent_version INT,
    canonical_claim JSONB NOT NULL,
    route JSONB NOT NULL,
    source_context JSONB NOT NULL,
    routing_context JSONB NOT NULL DEFAULT '{}',
    rebuild_reason TEXT,
    rebuild_attempt INT NOT NULL DEFAULT 0,
    is_current BOOLEAN NOT NULL DEFAULT FALSE,
    created_by_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, version)
);

-- Object store index. The actual payload/body still belongs in S3/MinIO/etc.
-- This table lets PostgreSQL know which object exists and why.
CREATE TABLE IF NOT EXISTS object_artifact (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT REFERENCES claim(claim_id) ON DELETE SET NULL,
    artifact_type TEXT NOT NULL,
    content_type TEXT NOT NULL,
    object_uri TEXT UNIQUE NOT NULL,
    sha256_hash TEXT,
    byte_size BIGINT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Built claim payloads
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS claim_payload (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    claim_version_id UUID REFERENCES claim_version(id) ON DELETE SET NULL,
    artifact_id UUID REFERENCES object_artifact(id) ON DELETE SET NULL,
    version INT NOT NULL,

    standard TEXT NOT NULL CHECK (standard IN ('NPHIES', 'ECLAIMLINK', 'SHAFAFIYA', 'CANONICAL')),
    payload_type TEXT NOT NULL CHECK (
        payload_type IN (
            'fhir_bundle_json',
            'xml',
            'application/xml',
            'text/xml',
            'application/json',
            'application/fhir+json'
        )
    ),
    object_uri TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,

    status TEXT NOT NULL,
    schema_status TEXT,
    validation_summary JSONB NOT NULL DEFAULT '{}',
    pre_auth_ref_embedded TEXT,
    generated_by_agent TEXT,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at TIMESTAMPTZ,
    superseded_by UUID REFERENCES claim_payload(id) ON DELETE SET NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, version)
);

CREATE TABLE IF NOT EXISTS payload_validation_result (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_payload_id UUID REFERENCES claim_payload(id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    validator TEXT NOT NULL,
    standard TEXT,
    schema_uri TEXT,
    status TEXT NOT NULL,
    parsed_payload JSONB,
    errors JSONB NOT NULL DEFAULT '[]',
    warnings JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Eligibility flow
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS eligibility_request (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    claim_version_id UUID REFERENCES claim_version(id) ON DELETE SET NULL,
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

-- Kept compatible with the current migration name and future repository method.
CREATE TABLE IF NOT EXISTS eligibility_check (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID REFERENCES eligibility_request(id) ON DELETE SET NULL,
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    patient_id TEXT,
    payer_id TEXT,
    plan_id TEXT,
    service_date DATE,

    status TEXT NOT NULL,
    outcome TEXT,
    coverage_ref TEXT,
    member_id TEXT,
    eligibility_ref TEXT,
    voi_ref TEXT,
    benefit_summary JSONB NOT NULL DEFAULT '{}',
    payer_response JSONB NOT NULL DEFAULT '{}',
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ttl_expires_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Prior authorization flow
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pa_payload (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    claim_version_id UUID REFERENCES claim_version(id) ON DELETE SET NULL,
    artifact_id UUID REFERENCES object_artifact(id) ON DELETE SET NULL,
    version INT NOT NULL,

    standard TEXT NOT NULL CHECK (standard IN ('NPHIES', 'SHAFAFIYA', 'ECLAIMLINK', 'CANONICAL')),
    payload_type TEXT NOT NULL CHECK (
        payload_type IN (
            'fhir_bundle_json',
            'xml',
            'application/xml',
            'text/xml',
            'application/json',
            'application/fhir+json'
        )
    ),
    object_uri TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,
    required_codes JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, version)
);

CREATE TABLE IF NOT EXISTS prior_auth_request (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    pa_payload_id UUID REFERENCES pa_payload(id) ON DELETE SET NULL,

    standard TEXT NOT NULL,
    payload_type TEXT,
    object_uri TEXT,
    sha256_hash TEXT,
    required_codes JSONB NOT NULL DEFAULT '[]',

    payer_id TEXT,
    plan_id TEXT,
    service_date DATE,
    status TEXT NOT NULL,
    outcome TEXT,
    external_transaction_id TEXT,
    portal_task_id TEXT,
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

CREATE TABLE IF NOT EXISTS prior_auth_response (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES prior_auth_request(id) ON DELETE CASCADE,
    payer_response JSONB NOT NULL,
    normalized_response JSONB NOT NULL DEFAULT '{}',

    status TEXT NOT NULL,
    outcome TEXT,
    decision TEXT,
    pre_auth_ref TEXT,
    payer_id TEXT,
    cpt_codes JSONB NOT NULL DEFAULT '[]',
    valid_from DATE,
    valid_to DATE,
    message TEXT,
    source TEXT NOT NULL DEFAULT 'WEBHOOK' CHECK (source IN ('WEBHOOK', 'POLL', 'MANUAL')),
    raw_payload_uri TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Validation reports
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS validation_report (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    claim_payload_id UUID REFERENCES claim_payload(id) ON DELETE SET NULL,
    version INT NOT NULL,
    score INT NOT NULL CHECK (score >= 0 AND score <= 100),
    final_status TEXT NOT NULL,
    report JSONB NOT NULL DEFAULT '{}',
    report_uri TEXT,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS validation_check (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id UUID NOT NULL REFERENCES validation_report(id) ON DELETE CASCADE,
    check_type TEXT NOT NULL,
    status TEXT NOT NULL,
    passes BOOLEAN NOT NULL DEFAULT FALSE,
    issue_count INT NOT NULL DEFAULT 0,
    data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS validation_issue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id UUID NOT NULL REFERENCES validation_report(id) ON DELETE CASCADE,
    check_id UUID REFERENCES validation_check(id) ON DELETE SET NULL,
    check_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('CRITICAL', 'ERROR', 'WARNING', 'INFO')),
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    suggestion TEXT,
    field TEXT,
    penalty INT NOT NULL DEFAULT 0,
    evidence JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Payer rules, KG/LLM enrichment references
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS payer_rule_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payer_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '1.0',
    source TEXT NOT NULL DEFAULT 'MOCK',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    jurisdiction TEXT,
    rule_set JSONB NOT NULL,
    effective_from DATE,
    effective_to DATE,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    eligibility_ttl_seconds INT NOT NULL DEFAULT 3600,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payer_rule (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_version_id UUID NOT NULL REFERENCES payer_rule_version(id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    payer_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'ERROR',
    max_deduction_per_layer INT,
    requires_llm BOOLEAN NOT NULL DEFAULT FALSE,
    requires_kg BOOLEAN NOT NULL DEFAULT FALSE,
    condition JSONB NOT NULL DEFAULT '{}',
    action JSONB NOT NULL DEFAULT '{}',
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, rule_id)
);

CREATE TABLE IF NOT EXISTS coding_enrichment_result (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    claim_version_id UUID REFERENCES claim_version(id) ON DELETE SET NULL,
    source TEXT NOT NULL CHECK (source IN ('KG', 'LLM', 'RULE_ENGINE')),
    model_or_graph_version TEXT,
    input_hash TEXT,
    result JSONB NOT NULL DEFAULT '{}',
    confidence NUMERIC(5, 4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Submission, callbacks, background jobs, checkpoints, idempotency
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS submission_attempt (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    claim_payload_id UUID REFERENCES claim_payload(id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    standard TEXT,
    payer_id TEXT,
    fingerprint TEXT,
    object_uri TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}',
    submitted_at TIMESTAMPTZ,
    response_status TEXT,
    payer_response JSONB NOT NULL DEFAULT '{}',
    response_uri TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT,
    agent TEXT NOT NULL,
    node TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    object_uri TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS bg_job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id TEXT UNIQUE NOT NULL,
    claim_id TEXT REFERENCES claim(claim_id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'WAITING',
    agent TEXT,
    node TEXT,
    thread_id TEXT,
    checkpoint_id TEXT,
    resume_node TEXT,
    payer_id TEXT,
    external_transaction_id TEXT,
    poll_attempt INT NOT NULL DEFAULT 0,
    backoff_schedule JSONB NOT NULL DEFAULT '[30,60,120,300,900,1800,3600]',
    next_poll_at TIMESTAMPTZ,
    waiting_since TIMESTAMPTZ,
    last_polled_at TIMESTAMPTZ,
    last_response JSONB NOT NULL DEFAULT '{}',
    locked_until TIMESTAMPTZ,
    escalated_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS graph_checkpoint (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    claim_id TEXT REFERENCES claim(claim_id) ON DELETE CASCADE,
    agent TEXT,
    resume_node TEXT,
    state JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS idempotency_record (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key TEXT UNIQUE NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROCESSING',
    request_hash TEXT,
    response JSONB,
    locked_until TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_claim_status ON claim(status);
CREATE INDEX IF NOT EXISTS idx_claim_payer_plan ON claim(payer_id, plan_id);
CREATE INDEX IF NOT EXISTS idx_claim_patient ON claim(patient_id);
CREATE INDEX IF NOT EXISTS idx_claim_service_date ON claim(service_date);
CREATE INDEX IF NOT EXISTS idx_claim_updated_at ON claim(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_claim_metadata_gin ON claim USING GIN (metadata);

CREATE INDEX IF NOT EXISTS idx_fhir_context_claim ON fhir_context_session(claim_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_fhir_resource_claim_type ON fhir_resource_snapshot(claim_id, resource_type);
CREATE INDEX IF NOT EXISTS idx_fhir_resource_json_gin ON fhir_resource_snapshot USING GIN (resource_json);

CREATE INDEX IF NOT EXISTS idx_route_claim_standard ON route_decision(claim_standard, jurisdiction);
CREATE INDEX IF NOT EXISTS idx_route_json_gin ON route_decision USING GIN (route);

CREATE INDEX IF NOT EXISTS idx_claim_version_claim_latest ON claim_version(claim_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_claim_version_current ON claim_version(claim_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS idx_claim_version_canonical_gin ON claim_version USING GIN (canonical_claim);

CREATE INDEX IF NOT EXISTS idx_object_claim_type ON object_artifact(claim_id, artifact_type);

CREATE INDEX IF NOT EXISTS idx_claim_payload_claim_latest ON claim_payload(claim_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_claim_payload_standard_status ON claim_payload(standard, status);
CREATE INDEX IF NOT EXISTS idx_payload_validation_claim ON payload_validation_result(claim_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_eligibility_request_claim ON eligibility_request(claim_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eligibility_request_waiting ON eligibility_request(next_poll_at) WHERE status IN ('WAITING_FOR_PAYER', 'QUEUED', 'PENDED', 'PARTIAL');
CREATE INDEX IF NOT EXISTS idx_eligibility_check_claim_latest ON eligibility_check(claim_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_eligibility_check_lookup ON eligibility_check(patient_id, payer_id, service_date);

CREATE INDEX IF NOT EXISTS idx_pa_payload_claim_latest ON pa_payload(claim_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_prior_auth_request_claim ON prior_auth_request(claim_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_prior_auth_request_waiting ON prior_auth_request(next_poll_at) WHERE status IN ('WAITING_FOR_PAYER', 'QUEUED', 'PENDED', 'PARTIAL', 'REQUIRED_MISSING');
CREATE INDEX IF NOT EXISTS idx_prior_auth_request_required_codes_gin ON prior_auth_request USING GIN (required_codes);
CREATE INDEX IF NOT EXISTS idx_prior_auth_response_request ON prior_auth_response(request_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_prior_auth_response_ref ON prior_auth_response(pre_auth_ref);
CREATE INDEX IF NOT EXISTS idx_prior_auth_response_codes_gin ON prior_auth_response USING GIN (cpt_codes);
CREATE INDEX IF NOT EXISTS idx_prior_auth_response_payload_gin ON prior_auth_response USING GIN (payer_response);

CREATE INDEX IF NOT EXISTS idx_validation_report_claim_latest ON validation_report(claim_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_validation_issue_report ON validation_issue(report_id);
CREATE INDEX IF NOT EXISTS idx_validation_issue_severity ON validation_issue(severity, check_type);

CREATE INDEX IF NOT EXISTS idx_payer_rule_version_lookup ON payer_rule_version(payer_id, plan_id, loaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_payer_rule_version_rules_gin ON payer_rule_version USING GIN (rule_set);
CREATE INDEX IF NOT EXISTS idx_payer_rule_lookup ON payer_rule(payer_id, plan_id, rule_type);
CREATE INDEX IF NOT EXISTS idx_payer_rule_condition_gin ON payer_rule USING GIN (condition);

CREATE INDEX IF NOT EXISTS idx_submission_claim ON submission_attempt(claim_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_submission_dedupe ON submission_attempt(payer_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_submission_response_gin ON submission_attempt USING GIN (payer_response);

CREATE INDEX IF NOT EXISTS idx_audit_claim_ts ON audit_event(claim_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent_node ON audit_event(agent, node, event_type);
CREATE INDEX IF NOT EXISTS idx_audit_payload_gin ON audit_event USING GIN (payload);

CREATE INDEX IF NOT EXISTS idx_callback_claim_received ON callback_event(claim_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_callback_job ON callback_event(job_id);
CREATE INDEX IF NOT EXISTS idx_callback_payload_gin ON callback_event USING GIN (raw_payload);

CREATE INDEX IF NOT EXISTS idx_bg_job_due ON bg_job(next_poll_at) WHERE status = 'WAITING';
CREATE INDEX IF NOT EXISTS idx_bg_job_claim ON bg_job(claim_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_checkpoint_thread ON graph_checkpoint(thread_id, checkpoint_id);
CREATE INDEX IF NOT EXISTS idx_idempotency_scope ON idempotency_record(scope, created_at DESC);

-- ---------------------------------------------------------------------------
-- Updated-at triggers
-- ---------------------------------------------------------------------------

DROP TRIGGER IF EXISTS trg_claim_updated_at ON claim;
CREATE TRIGGER trg_claim_updated_at BEFORE UPDATE ON claim
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_fhir_context_session_updated_at ON fhir_context_session;
CREATE TRIGGER trg_fhir_context_session_updated_at BEFORE UPDATE ON fhir_context_session
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_route_decision_updated_at ON route_decision;
CREATE TRIGGER trg_route_decision_updated_at BEFORE UPDATE ON route_decision
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_claim_version_updated_at ON claim_version;
CREATE TRIGGER trg_claim_version_updated_at BEFORE UPDATE ON claim_version
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_claim_payload_updated_at ON claim_payload;
CREATE TRIGGER trg_claim_payload_updated_at BEFORE UPDATE ON claim_payload
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_eligibility_request_updated_at ON eligibility_request;
CREATE TRIGGER trg_eligibility_request_updated_at BEFORE UPDATE ON eligibility_request
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_eligibility_check_updated_at ON eligibility_check;
CREATE TRIGGER trg_eligibility_check_updated_at BEFORE UPDATE ON eligibility_check
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_pa_payload_updated_at ON pa_payload;
CREATE TRIGGER trg_pa_payload_updated_at BEFORE UPDATE ON pa_payload
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_prior_auth_request_updated_at ON prior_auth_request;
CREATE TRIGGER trg_prior_auth_request_updated_at BEFORE UPDATE ON prior_auth_request
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_prior_auth_response_updated_at ON prior_auth_response;
CREATE TRIGGER trg_prior_auth_response_updated_at BEFORE UPDATE ON prior_auth_response
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_validation_report_updated_at ON validation_report;
CREATE TRIGGER trg_validation_report_updated_at BEFORE UPDATE ON validation_report
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_validation_issue_updated_at ON validation_issue;
CREATE TRIGGER trg_validation_issue_updated_at BEFORE UPDATE ON validation_issue
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_payer_rule_version_updated_at ON payer_rule_version;
CREATE TRIGGER trg_payer_rule_version_updated_at BEFORE UPDATE ON payer_rule_version
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_submission_attempt_updated_at ON submission_attempt;
CREATE TRIGGER trg_submission_attempt_updated_at BEFORE UPDATE ON submission_attempt
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_audit_event_updated_at ON audit_event;
CREATE TRIGGER trg_audit_event_updated_at BEFORE UPDATE ON audit_event
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_callback_event_updated_at ON callback_event;
CREATE TRIGGER trg_callback_event_updated_at BEFORE UPDATE ON callback_event
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_bg_job_updated_at ON bg_job;
CREATE TRIGGER trg_bg_job_updated_at BEFORE UPDATE ON bg_job
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_graph_checkpoint_updated_at ON graph_checkpoint;
CREATE TRIGGER trg_graph_checkpoint_updated_at BEFORE UPDATE ON graph_checkpoint
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_idempotency_record_updated_at ON idempotency_record;
CREATE TRIGGER trg_idempotency_record_updated_at BEFORE UPDATE ON idempotency_record
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Frontend / operations read model
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW claim_queue_view AS
WITH latest_payload AS (
    SELECT DISTINCT ON (claim_id)
        claim_id,
        id AS claim_payload_id,
        version,
        standard,
        payload_type,
        object_uri,
        sha256_hash,
        status AS payload_status,
        schema_status,
        generated_at
    FROM claim_payload
    ORDER BY claim_id, version DESC
),
latest_report AS (
    SELECT DISTINCT ON (claim_id)
        claim_id,
        id AS validation_report_id,
        score,
        final_status,
        report,
        generated_at AS validation_generated_at
    FROM validation_report
    ORDER BY claim_id, created_at DESC
),
latest_eligibility AS (
    SELECT DISTINCT ON (claim_id)
        claim_id,
        status AS eligibility_status,
        coverage_ref,
        member_id,
        eligibility_ref,
        voi_ref,
        checked_at
    FROM eligibility_check
    ORDER BY claim_id, checked_at DESC
),
latest_pa AS (
    SELECT DISTINCT ON (q.claim_id)
        q.claim_id,
        q.id AS prior_auth_request_id,
        q.status AS prior_auth_request_status,
        r.status AS prior_auth_response_status,
        r.pre_auth_ref,
        COALESCE(r.received_at, q.created_at) AS prior_auth_updated_at
    FROM prior_auth_request q
    LEFT JOIN prior_auth_response r ON r.request_id = q.id
    ORDER BY q.claim_id, COALESCE(r.received_at, q.created_at) DESC
)
SELECT
    c.claim_id,
    c.status,
    c.jurisdiction,
    COALESCE(lp.standard, c.claim_standard) AS claim_standard,
    c.payer_id,
    c.payer_name,
    c.plan_id,
    c.patient_id,
    c.patient_name,
    c.provider_id,
    c.facility_id,
    c.encounter_id,
    c.service_date,
    c.currency,
    c.total_amount,
    COALESCE(lr.score, 0) AS validation_score,
    lr.final_status AS validation_status,
    lp.claim_payload_id,
    lp.version AS payload_version,
    lp.payload_type,
    lp.object_uri AS claim_payload_uri,
    lp.sha256_hash AS claim_payload_hash,
    lp.payload_status,
    lp.schema_status,
    le.eligibility_status,
    le.coverage_ref,
    le.eligibility_ref,
    le.voi_ref,
    lpa.prior_auth_request_status,
    lpa.prior_auth_response_status,
    lpa.pre_auth_ref,
    c.updated_at
FROM claim c
LEFT JOIN latest_payload lp ON lp.claim_id = c.claim_id
LEFT JOIN latest_report lr ON lr.claim_id = c.claim_id
LEFT JOIN latest_eligibility le ON le.claim_id = c.claim_id
LEFT JOIN latest_pa lpa ON lpa.claim_id = c.claim_id;
