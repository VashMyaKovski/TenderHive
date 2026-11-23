DO $$
DECLARE constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'review_cards'::regclass AND contype = 'c'
    LOOP
        EXECUTE format('ALTER TABLE review_cards DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

ALTER TABLE review_cards
    ADD COLUMN IF NOT EXISTS decided_at timestamptz,
    ADD COLUMN IF NOT EXISTS applied boolean NOT NULL DEFAULT false;

UPDATE review_cards SET decided_at = CURRENT_TIMESTAMP
    WHERE state = 'decided' AND decided_at IS NULL;

ALTER TABLE review_cards
    ADD CONSTRAINT review_cards_step_check CHECK (step IN (
        'risk_review', 'risk_escalation',
        'margin_review', 'margin_escalation',
        'resource_review', 'resource_escalation',
        'no_solution_review', 'no_solution_escalation',
        'final_approval_review', 'final_approval_escalation'
    )),
    ADD CONSTRAINT review_cards_reason_code_check CHECK (reason_code IN (
        'penalty_above_threshold', 'unclear_requirements', 'low_margin',
        'resource_gap', 'no_solution', 'final_approval'
    )),
    ADD CONSTRAINT review_cards_role_check CHECK (role IN (
        'юрист', 'руководитель подразделения', 'служба снабжения', 'тендерный специалист'
    )),
    ADD CONSTRAINT review_cards_state_check CHECK (state IN ('open', 'decided', 'timed_out')),
    ADD CONSTRAINT review_cards_timeout_check CHECK (
        state <> 'timed_out' OR step NOT LIKE '%escalation'
    ),
    ADD CONSTRAINT review_cards_decision_check CHECK (
        (state = 'decided' AND decision_id IS NOT NULL AND actor_id IS NOT NULL
            AND action IS NOT NULL AND comment IS NOT NULL AND decided_at IS NOT NULL)
        OR (state <> 'decided' AND decision_id IS NULL AND actor_id IS NULL
            AND action IS NULL AND comment IS NULL AND decided_at IS NULL)
    ),
    ADD CONSTRAINT review_cards_stop_comment_check CHECK (action <> 'stop' OR length(comment) > 0);

DROP INDEX IF EXISTS review_cards_due_idx;
CREATE INDEX review_cards_due_idx ON review_cards (deadline, job_id)
    WHERE state = 'open' AND step NOT LIKE '%escalation';

CREATE INDEX review_cards_unapplied_idx ON review_cards (job_id, decided_at)
    WHERE state = 'decided' AND NOT applied;
