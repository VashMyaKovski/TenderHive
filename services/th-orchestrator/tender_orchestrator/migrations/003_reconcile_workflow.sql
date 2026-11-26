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
ALTER TABLE review_cards DROP CONSTRAINT review_cards_state_check;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_job_id_step_key;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_timeout_check;
ALTER TABLE review_cards DROP CONSTRAINT review_cards_decision_check;
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
ALTER TABLE review_cards ADD COLUMN decided_role text;
ALTER TABLE review_cards ADD COLUMN question text;
ALTER TABLE review_cards ADD COLUMN supply_confirmation jsonb;
ALTER TABLE review_cards ADD COLUMN review_round integer NOT NULL DEFAULT 0 CHECK (review_round >= 0);
ALTER TABLE review_cards ADD COLUMN opened_at timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE review_cards ADD CONSTRAINT review_cards_decision_check CHECK (
    (state = 'decided' AND decision_id IS NOT NULL AND actor_id IS NOT NULL
        AND action IS NOT NULL AND comment IS NOT NULL)
    OR (state <> 'decided' AND decision_id IS NULL AND actor_id IS NULL
        AND action IS NULL AND comment IS NULL AND decided_at IS NULL)
);

-- Retain outstanding legacy reviews. An unapplied decision is advanced by the same recovery
-- worker as a new decision. A legacy review sequence still returns to the solver afterward.
WITH current_review AS (
    SELECT DISTINCT ON (job_id) job_id, reason_code, state
    FROM review_cards
    WHERE step NOT IN ('risk_review', 'risk_escalation')
      AND (state = 'open' OR (state = 'decided' AND NOT applied))
    ORDER BY job_id, (state = 'open') DESC, decided_at DESC, card_id
)
UPDATE jobs AS job
SET stage = 'review',
    review_round = 1,
    needs_advance = current_review.state = 'decided',
    pending_reasons = ARRAY[upper(current_review.reason_code)] || ARRAY(
        SELECT upper(point.value->>'reason_code')
        FROM jsonb_array_elements(COALESCE(job.request->'reviews', '[]'::jsonb))
            WITH ORDINALITY AS point(value, sequence)
        WHERE point.value->>'reason_code' <> current_review.reason_code
          AND NOT EXISTS (
              SELECT 1 FROM review_cards AS seen
              WHERE seen.job_id = job.job_id AND seen.state = 'decided'
                AND seen.reason_code = point.value->>'reason_code'
          )
        ORDER BY point.sequence
    )
FROM current_review
WHERE current_review.job_id = job.job_id AND job.phase = 'awaiting_review';

-- Risk graphs start in a new checkpoint namespace. The old checkpoint can reference nodes
-- removed from the new graph. The persisted assessment and card are used during resumption.
UPDATE jobs
SET risk_generation = 1, needs_advance = true
WHERE stage = 'risk' AND phase IN ('accepted', 'assessing', 'awaiting_review');

UPDATE jobs SET needs_advance = false WHERE phase IN ('ready_for_next_stage', 'declined', 'failed');

UPDATE review_cards AS card
SET review_round = CASE
        WHEN card.step IN ('risk_review', 'risk_escalation') THEN job.risk_generation
        ELSE 1
    END,
    opened_at = LEAST(COALESCE(card.decided_at, card.deadline), transaction_timestamp())
FROM jobs AS job
WHERE job.job_id = card.job_id;

UPDATE review_cards
SET step = CASE
        WHEN step IN ('risk_review', 'risk_escalation') THEN step
        WHEN step IN ('margin_escalation', 'resource_escalation', 'no_solution_escalation',
                      'final_approval_escalation') THEN 'review_escalation'
        ELSE 'review'
    END,
    reason_code = upper(reason_code),
    role = CASE role
        WHEN 'юрист' THEN 'LAWYER'
        WHEN 'руководитель подразделения' THEN 'DIVISION_HEAD'
        WHEN 'служба снабжения' THEN 'SUPPLY'
        WHEN 'тендерный специалист' THEN 'TENDER_SPECIALIST'
        ELSE role
    END;

UPDATE review_cards SET decided_role = role WHERE state = 'decided';

ALTER TABLE review_cards ADD CONSTRAINT review_cards_step_check CHECK (
    step IN ('risk_review', 'risk_escalation', 'review', 'review_escalation')
);
ALTER TABLE review_cards ADD CONSTRAINT review_cards_reason_code_check CHECK (reason_code IN (
    'penalty_above_threshold', 'unclear_requirements', 'PENALTY_ABOVE_THRESHOLD', 'LOW_MARGIN',
    'RESOURCE_GAP', 'UNCLEAR_REQUIREMENTS', 'NO_SOLUTION', 'FINAL_APPROVAL'
));
ALTER TABLE review_cards ADD UNIQUE (job_id, step, review_round, reason_code);
ALTER TABLE review_cards ADD CONSTRAINT escalation_card_open_or_decided CHECK (
    step NOT IN ('risk_escalation', 'review_escalation') OR state <> 'timed_out'
);
ALTER TABLE review_cards ADD CONSTRAINT clarification_comment CHECK (
    action <> 'needs_info' OR length(comment) > 0
);
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
