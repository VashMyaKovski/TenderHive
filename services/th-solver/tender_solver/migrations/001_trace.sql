CREATE SCHEMA IF NOT EXISTS th_solver;

CREATE TABLE IF NOT EXISTS th_solver.traces (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL,
    trace_id uuid NOT NULL,
    kb_version text NOT NULL,
    status text NOT NULL,
    stop_reasons text[] NOT NULL,
    cached boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS solver_job_trace_idx ON th_solver.traces (job_id, trace_id);
