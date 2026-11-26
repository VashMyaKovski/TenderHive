CREATE TABLE workbench_runs (
    run_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL,
    pending boolean NOT NULL,
    snapshot jsonb NOT NULL
);

CREATE INDEX workbench_runs_pending_idx ON workbench_runs (created_at) WHERE pending;
