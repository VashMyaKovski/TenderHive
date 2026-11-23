CREATE SCHEMA IF NOT EXISTS kb;

CREATE TABLE IF NOT EXISTS kb.versions (
    version TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'CHECK', 'ACTIVE', 'REJECTED', 'OLD')),
    payload JSONB NOT NULL,
    checksum TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS versions_active_idx
    ON kb.versions ((status = 'ACTIVE')) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS kb.activation_log (
    id BIGSERIAL PRIMARY KEY,
    version TEXT NOT NULL REFERENCES kb.versions(version),
    actor TEXT NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
