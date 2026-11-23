ALTER TABLE jobs DROP CONSTRAINT jobs_phase_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_phase_check CHECK (phase IN (
    'accepted', 'assessing', 'awaiting_review', 'ready_for_next_stage', 'needs_clarification',
    'awaiting_artifacts', 'done', 'declined', 'failed'
));
ALTER TABLE jobs DROP CONSTRAINT jobs_check;
ALTER TABLE jobs ADD COLUMN stage text NOT NULL DEFAULT 'risk'
    CHECK (stage IN ('extract', 'risk', 'solve', 'review', 'artifacts'));
ALTER TABLE jobs ADD COLUMN execution_id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE jobs ADD COLUMN risk_generation integer NOT NULL DEFAULT 0 CHECK (risk_generation >= 0);
ALTER TABLE jobs ADD COLUMN risk_input jsonb;
ALTER TABLE jobs ADD COLUMN solver_result jsonb;
ALTER TABLE jobs ADD COLUMN artifacts jsonb;
ALTER TABLE jobs ADD COLUMN review_round integer NOT NULL DEFAULT 0 CHECK (review_round >= 0);
ALTER TABLE jobs ADD COLUMN pending_reasons text[] NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN updated_at timestamptz;
ALTER TABLE jobs ADD COLUMN status_version bigint NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN status_synced_version bigint NOT NULL DEFAULT -1;
ALTER TABLE jobs ADD COLUMN cache_synced_at timestamptz;
CREATE INDEX jobs_status_sync_idx ON jobs (job_id) WHERE status_synced_version < status_version;
CREATE INDEX jobs_active_cache_idx ON jobs (cache_synced_at NULLS FIRST, job_id)
    WHERE phase NOT IN ('done', 'declined', 'failed');
ALTER TABLE jobs ADD CONSTRAINT job_completion_prerequisites CHECK (
    phase <> 'done' OR (stage = 'artifacts' AND solver_result IS NOT NULL AND artifacts IS NOT NULL)
);
UPDATE jobs SET stage = 'solve' WHERE phase = 'ready_for_next_stage';

ALTER TABLE review_cards DROP CONSTRAINT review_cards_step_check;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_reason_code_check;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_role_check;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_action_check;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_state_check;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_job_id_step_key;
ALTER TABLE review_cards ADD CONSTRAINT review_cards_step_check CHECK (
    step IN ('risk_review', 'risk_escalation', 'review', 'review_escalation')
);
ALTER TABLE review_cards ADD CONSTRAINT review_cards_reason_code_check CHECK (reason_code IN (
    'penalty_above_threshold', 'unclear_requirements', 'PENALTY_ABOVE_THRESHOLD', 'LOW_MARGIN',
    'RESOURCE_GAP', 'UNCLEAR_REQUIREMENTS', 'NO_SOLUTION', 'FINAL_APPROVAL'
));
ALTER TABLE review_cards ADD CONSTRAINT review_cards_role_check CHECK (role IN (
    'юрист', 'руководитель подразделения', 'служба снабжения', 'тендерный специалист',
    'LAWYER', 'DIVISION_HEAD', 'SUPPLY', 'TENDER_SPECIALIST', 'ADMINISTRATOR'
));
ALTER TABLE review_cards ADD CONSTRAINT review_cards_action_check CHECK (
    action IN ('continue', 'stop', 'needs_info')
);
ALTER TABLE review_cards ADD CONSTRAINT review_cards_state_check CHECK (
    state IN ('open', 'decided', 'timed_out', 'cancelled')
);
ALTER TABLE review_cards ADD COLUMN decided_at timestamptz;
ALTER TABLE review_cards ADD COLUMN decided_role text;
ALTER TABLE review_cards ADD COLUMN question text;
ALTER TABLE review_cards ADD COLUMN review_round integer NOT NULL DEFAULT 0 CHECK (review_round >= 0);
ALTER TABLE review_cards ADD COLUMN opened_at timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE review_cards ADD UNIQUE (job_id, step, review_round, reason_code);
ALTER TABLE review_cards ADD CONSTRAINT escalation_card_open_or_decided CHECK (
    step NOT IN ('risk_escalation', 'review_escalation') OR state <> 'timed_out'
);
ALTER TABLE review_cards ADD CONSTRAINT clarification_comment CHECK (
    action <> 'needs_info' OR length(comment) > 0
);
DO $$
DECLARE timed_out_constraint name;
BEGIN
    SELECT conname INTO STRICT timed_out_constraint FROM pg_constraint
    WHERE conrelid = 'review_cards'::regclass AND contype = 'c'
        AND pg_get_constraintdef(oid) LIKE '%timed_out%risk_review%';
    EXECUTE format('ALTER TABLE review_cards DROP CONSTRAINT %I', timed_out_constraint);
END $$;
DROP INDEX review_cards_due_idx;
CREATE INDEX review_cards_due_idx ON review_cards (deadline, job_id)
    WHERE state = 'open' AND step IN ('risk_review', 'review');

CREATE TABLE stage_receipts (
    job_id uuid NOT NULL REFERENCES jobs (job_id),
    execution_id uuid NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (job_id, execution_id, kind)
);

CREATE TABLE event_outbox (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    job_id uuid NOT NULL REFERENCES jobs (job_id),
    topic text NOT NULL,
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    delivered boolean NOT NULL DEFAULT false
);
CREATE INDEX event_outbox_pending_idx ON event_outbox (sequence) WHERE NOT delivered;

CREATE TABLE rejected_events (
    topic text NOT NULL,
    partition_id integer NOT NULL,
    broker_offset bigint NOT NULL,
    reason text NOT NULL,
    rejected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (topic, partition_id, broker_offset)
);
