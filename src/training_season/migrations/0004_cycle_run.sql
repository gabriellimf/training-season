-- One row per run of the weekly cycle command. A row with no end belongs to a
-- run that is still going, or to a process that was killed before it ended.
-- Applied by training_season.database.migrate inside one BEGIN IMMEDIATE
-- transaction. Do not add BEGIN, COMMIT, ROLLBACK, SAVEPOINT or PRAGMA
-- statements here. The runner owns transaction control and pragmas.

CREATE TABLE cycle_run (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
    started_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    ended_at TEXT
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', ended_at) IS ended_at),
    outcome TEXT
        CHECK (outcome IN ('done', 'failed', 'crashed', 'interrupted', 'refused')),
    exit_code INTEGER,
    message_id INTEGER REFERENCES message (id),
    CHECK ((ended_at IS NULL AND outcome IS NULL AND exit_code IS NULL)
        OR (ended_at IS NOT NULL AND outcome IS NOT NULL
            AND exit_code IS NOT NULL))
) STRICT;

CREATE INDEX cycle_run_open ON cycle_run (id) WHERE ended_at IS NULL;
