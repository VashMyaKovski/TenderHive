create table if not exists users (
    user_id uuid primary key,
    username text not null unique,
    display_name text not null,
    role text not null,
    password_hash text not null
);

create table if not exists kb_versions (
    version text primary key,
    status text not null,
    created_at timestamptz not null,
    activated_at timestamptz null,
    activated_by text null,
    rules_count integer not null default 0,
    notes text null
);

create table if not exists kb_activations (
    id bigserial primary key,
    version text not null,
    action text not null,
    actor text not null,
    occurred_at timestamptz not null,
    comment text null
);

create table if not exists jobs (
    job_id uuid primary key,
    idempotency_key text not null unique,
    source text not null,
    created_at timestamptz not null,
    status text not null,
    kb_version text not null,
    lot_ids jsonb not null default '[]'::jsonb,
    profile_ref text not null default '',
    telegram_chat_id text null,
    recommendation text null,
    margin_percent double precision null,
    artifacts_delivered_at timestamptz null
);

create table if not exists job_documents (
    document_id uuid primary key,
    job_id uuid not null references jobs (job_id) on delete cascade,
    file_name text not null,
    content_type text not null,
    size bigint not null,
    sha256 text not null,
    storage_path text not null
);

create index if not exists ix_job_documents_job on job_documents (job_id);

create table if not exists job_steps (
    step_id uuid primary key,
    job_id uuid not null references jobs (job_id) on delete cascade,
    code text not null,
    title text not null,
    state text not null,
    started_at timestamptz not null,
    finished_at timestamptz null,
    detail text null
);

create index if not exists ix_job_steps_job on job_steps (job_id, started_at);

create table if not exists job_results (
    job_id uuid primary key references jobs (job_id) on delete cascade,
    payload jsonb not null,
    created_at timestamptz not null
);

create table if not exists artifacts (
    artifact_id uuid primary key,
    job_id uuid not null references jobs (job_id) on delete cascade,
    kind text not null,
    file_name text not null,
    content_type text not null,
    version integer not null,
    size bigint not null,
    sha256 text not null,
    storage_path text not null,
    created_at timestamptz not null
);

create index if not exists ix_artifacts_job on artifacts (job_id, kind, version);

create table if not exists human_decisions (
    decision_id uuid primary key,
    job_id uuid not null references jobs (job_id) on delete cascade,
    reason_code text not null,
    role text not null,
    created_at timestamptz not null,
    deadline timestamptz not null,
    timeout_hours double precision not null,
    decision text not null default 'pending',
    comment text null,
    decided_by text null,
    decided_at timestamptz null
);

create index if not exists ix_decisions_job on human_decisions (job_id, created_at);

create index if not exists ix_decisions_open on human_decisions (deadline) where decision = 'pending';

create table if not exists outbox_events (
    event_id bigserial primary key,
    topic text not null,
    payload jsonb not null,
    created_at timestamptz not null,
    published_at timestamptz null,
    attempts integer not null default 0,
    last_error text null
);

create index if not exists ix_outbox_pending on outbox_events (created_at) where published_at is null;

create table if not exists audit_entries (
    id bigserial primary key,
    occurred_at timestamptz not null,
    actor text not null,
    role text not null,
    action text not null,
    job_id uuid null,
    details text null
);

create index if not exists ix_audit_job on audit_entries (job_id, occurred_at);
