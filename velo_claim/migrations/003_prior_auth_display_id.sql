ALTER TABLE prior_auth_request
    ADD COLUMN display_id text;

CREATE UNIQUE INDEX idx_prior_auth_request_display_id ON prior_auth_request (display_id);