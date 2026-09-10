-- Align legacy PostgreSQL installations that still constrain audit events
-- with audit_event_type_enum. Fresh installations store this column as TEXT.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_type
        WHERE typname = 'audit_event_type_enum'
    ) THEN
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_CYCLE_CREATED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_SUGGESTION_CREATED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_MANUAL_RECONCILIATION_REQUIRED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_REVIEW_APPROVED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_REVIEW_MODIFIED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_REVIEW_REJECTED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_SUGGESTION_STALE';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_CYCLE_APPLIED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CLAIM_VERSION_CREATED_FROM_CORRECTION';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_REVALIDATION_COMPLETED';
        ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'CORRECTION_CYCLE_EXHAUSTED';
    END IF;
END
$$;
