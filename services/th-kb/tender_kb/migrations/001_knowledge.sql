CREATE TABLE IF NOT EXISTS th_kb_versions (
    version text PRIMARY KEY,
    checksum text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('DRAFT', 'CHECK', 'ACTIVE', 'REJECTED', 'OLD')),
    projection uuid UNIQUE,
    report jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((status IN ('CHECK', 'ACTIVE', 'OLD')) = (projection IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS th_kb_single_active
    ON th_kb_versions ((1)) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS th_kb_head (
    singleton boolean PRIMARY KEY CHECK (singleton),
    version text REFERENCES th_kb_versions (version),
    revision bigint NOT NULL DEFAULT 0
);

INSERT INTO th_kb_head (singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS th_kb_activation_journal (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version text NOT NULL REFERENCES th_kb_versions (version),
    previous_version text REFERENCES th_kb_versions (version),
    action text NOT NULL CHECK (action IN ('ACTIVATE', 'REJECT', 'ROLLBACK')),
    report jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
