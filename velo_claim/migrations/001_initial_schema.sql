CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE claim (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL,
    jurisdiction TEXT,
    payer_id TEXT,
    provider_id TEXT,
    patient_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE claim_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    version INT NOT NULL,
    canonical_claim JSONB NOT NULL,
    route JSONB NOT NULL,
    source_context JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, version)
);

CREATE TABLE route_decision (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT UNIQUE NOT NULL REFERENCES claim(claim_id),
    route JSONB NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE claim_payload (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    version INT NOT NULL,
    standard TEXT NOT NULL CHECK (standard IN ('NPHIES', 'ECLAIMLINK', 'SHAFAFIYA')),
    payload_type TEXT NOT NULL CHECK (payload_type IN ('fhir_bundle_json', 'xml', 'application/xml')),
    object_uri TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, version)
);

CREATE TABLE pa_payload (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    version INT NOT NULL,
    standard TEXT NOT NULL CHECK (standard IN ('NPHIES', 'SHAFAFIYA', 'ECLAIMLINK')),
    payload_type TEXT NOT NULL CHECK (payload_type IN ('fhir_bundle_json', 'xml', 'application/xml')),
    object_uri TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, version)
);

CREATE TABLE validation_report (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    version INT NOT NULL,
    score INT NOT NULL,
    final_status TEXT NOT NULL,
    report JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE validation_issue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id UUID NOT NULL REFERENCES validation_report(id),
    check_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    field TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE prior_auth_request (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    standard TEXT NOT NULL,
    object_uri TEXT,
    submitted_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE prior_auth_response (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES prior_auth_request(id),
    payer_response JSONB NOT NULL,
    pre_auth_ref TEXT,
    status TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE eligibility_check (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    patient_id TEXT,
    payer_id TEXT,
    service_date DATE,
    status TEXT NOT NULL,
    coverage_ref TEXT,
    member_id TEXT,
    benefit_summary JSONB NOT NULL DEFAULT '{}',
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ttl_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE payer_rule_version (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payer_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    rule_set JSONB NOT NULL,
    effective_from DATE,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    eligibility_ttl_seconds INT NOT NULL DEFAULT 3600,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE submission_attempt (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    channel TEXT NOT NULL,
    object_uri TEXT,
    submitted_at TIMESTAMPTZ,
    response_status TEXT,
    payer_response JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT,
    agent TEXT NOT NULL,
    node TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE callback_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id TEXT NOT NULL REFERENCES claim(claim_id),
    job_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('WEBHOOK', 'POLL', 'MANUAL')),
    raw_payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
