CREATE TABLE th_eval_datasets (
    suite text NOT NULL,
    kb_version text NOT NULL,
    digest text NOT NULL CHECK (length(digest) = 64),
    PRIMARY KEY (suite, kb_version)
);

CREATE TABLE th_eval_cases (
    suite text NOT NULL,
    kb_version text NOT NULL,
    case_id uuid NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    definition jsonb NOT NULL,
    PRIMARY KEY (suite, kb_version, case_id),
    UNIQUE (suite, kb_version, position),
    FOREIGN KEY (suite, kb_version) REFERENCES th_eval_datasets (suite, kb_version)
);

CREATE TABLE th_eval_runs (
    run_id uuid PRIMARY KEY,
    suite text NOT NULL,
    kb_version text NOT NULL,
    request jsonb NOT NULL,
    result jsonb NOT NULL,
    observations jsonb NOT NULL,
    FOREIGN KEY (suite, kb_version) REFERENCES th_eval_datasets (suite, kb_version)
);
