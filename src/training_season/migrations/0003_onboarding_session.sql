-- Onboarding sessions and the chat turns that belong to them.
-- Applied by training_season.database.migrate inside one BEGIN IMMEDIATE
-- transaction. Do not add BEGIN, COMMIT, ROLLBACK, SAVEPOINT or PRAGMA
-- statements here. The runner owns transaction control and pragmas.

CREATE TABLE onboarding_session (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'discarded')),
    started_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    completed_at TEXT
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', completed_at) IS completed_at),
    discarded_at TEXT
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', discarded_at) IS discarded_at),
    macro_plan_id INTEGER REFERENCES macro_plan (id),
    CHECK ((status = 'open' AND completed_at IS NULL AND discarded_at IS NULL
            AND macro_plan_id IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL
            AND discarded_at IS NULL AND macro_plan_id IS NOT NULL)
        OR (status = 'discarded' AND discarded_at IS NOT NULL
            AND completed_at IS NULL AND macro_plan_id IS NULL))
) STRICT;

CREATE UNIQUE INDEX onboarding_session_single_open
    ON onboarding_session (status) WHERE status = 'open';

ALTER TABLE message
    ADD COLUMN onboarding_session_id INTEGER REFERENCES onboarding_session (id);

CREATE INDEX message_onboarding_session ON message (onboarding_session_id);
