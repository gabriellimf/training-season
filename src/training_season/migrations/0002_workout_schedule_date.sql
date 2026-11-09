-- Index for the week push, which selects workout rows by schedule date range.
-- Applied by training_season.database.migrate inside one BEGIN IMMEDIATE
-- transaction. Do not add BEGIN, COMMIT, ROLLBACK, SAVEPOINT or PRAGMA
-- statements here. The runner owns transaction control and pragmas.

CREATE INDEX workout_schedule_date ON workout (schedule_date);
