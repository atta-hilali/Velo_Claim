-- Align legacy claim tables with the current intake model.
-- Imported encounters may be persisted for RCM review before every external
-- payer/provider identifier has been resolved.

ALTER TABLE claim ALTER COLUMN jurisdiction DROP NOT NULL;
ALTER TABLE claim ALTER COLUMN payer_id DROP NOT NULL;
ALTER TABLE claim ALTER COLUMN provider_id DROP NOT NULL;
ALTER TABLE claim ALTER COLUMN patient_id DROP NOT NULL;
