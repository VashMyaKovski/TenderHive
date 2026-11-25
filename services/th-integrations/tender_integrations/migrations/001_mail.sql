CREATE SCHEMA IF NOT EXISTS tender_integrations;

CREATE TABLE IF NOT EXISTS tender_integrations.mail_job (
    job_id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    sent_at timestamptz NOT NULL,
    sender text NOT NULL,
    subject text NOT NULL
);

CREATE TABLE IF NOT EXISTS tender_integrations.document (
    file_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES tender_integrations.mail_job (job_id) ON DELETE CASCADE,
    name text NOT NULL,
    mime text NOT NULL,
    content bytea NOT NULL
);

CREATE TABLE IF NOT EXISTS tender_integrations.outbox (
    job_id uuid PRIMARY KEY REFERENCES tender_integrations.mail_job (job_id) ON DELETE CASCADE,
    headers jsonb NOT NULL,
    queued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_token uuid,
    lease_until timestamptz,
    published_at timestamptz
);

CREATE INDEX IF NOT EXISTS integrations_outbox_pending
    ON tender_integrations.outbox (queued_at, job_id)
    WHERE published_at IS NULL;


CREATE INDEX IF NOT EXISTS integrations_job_external_ref
    ON tender_integrations.mail_job ((payload->>'external_ref'));
