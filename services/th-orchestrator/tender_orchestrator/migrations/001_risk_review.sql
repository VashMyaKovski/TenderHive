CREATE TABLE jobs (
    job_id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    request jsonb NOT NULL,
    phase text NOT NULL DEFAULT 'accepted' CHECK (phase IN (
        'accepted', 'assessing', 'awaiting_review', 'ready_for_next_stage', 'declined', 'failed'
    )),
    needs_advance boolean NOT NULL DEFAULT true,
    risk_attempts integer NOT NULL DEFAULT 0 CHECK (risk_attempts >= 0),
    risk_result jsonb,
    failure jsonb,
    CHECK (risk_result IS NULL OR failure IS NULL)
);

CREATE INDEX jobs_pending_idx ON jobs (job_id) WHERE needs_advance;

CREATE TABLE review_cards (
    card_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs (job_id),
    step text NOT NULL CHECK (step IN ('risk_review', 'risk_escalation')),
    reason_code text NOT NULL CHECK (reason_code IN ('penalty_above_threshold', 'unclear_requirements')),
    role text NOT NULL CHECK (role IN (
        'юрист', 'руководитель подразделения', 'служба снабжения', 'тендерный специалист'
    )),
    deadline timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'decided', 'timed_out')),
    decision_id uuid UNIQUE,
    actor_id uuid,
    action text CHECK (action IN ('continue', 'stop')),
    comment text,
    UNIQUE (job_id, step),
    CHECK (state <> 'timed_out' OR step = 'risk_review'),
    CHECK (
        (state = 'decided' AND decision_id IS NOT NULL AND actor_id IS NOT NULL
            AND action IS NOT NULL AND comment IS NOT NULL)
        OR (state <> 'decided' AND decision_id IS NULL AND actor_id IS NULL
            AND action IS NULL AND comment IS NULL)
    ),
    CHECK (action <> 'stop' OR length(comment) > 0)
);

CREATE UNIQUE INDEX review_cards_open_idx ON review_cards (job_id) WHERE state = 'open';
CREATE INDEX review_cards_due_idx ON review_cards (deadline, job_id)
    WHERE state = 'open' AND step = 'risk_review';
