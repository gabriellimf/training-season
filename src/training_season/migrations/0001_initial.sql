-- Initial schema. Applied by training_season.database.migrate inside one
-- BEGIN IMMEDIATE transaction. Do not add BEGIN, COMMIT, ROLLBACK, SAVEPOINT
-- or PRAGMA statements here. The runner owns transaction control and pragmas.
--
-- Timestamps: UTC, text, exactly YYYY-MM-DDTHH:MM:SSZ.
-- Training dates: text, exactly YYYY-MM-DD, computed in the training timezone.

CREATE TABLE profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data_json TEXT NOT NULL
        CHECK (CASE WHEN json_valid(data_json) THEN json_type(data_json) = 'object' ELSE 0 END),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    updated_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
    CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE macro_plan (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    start_date TEXT NOT NULL CHECK (date(start_date) IS start_date),
    race_date TEXT NOT NULL CHECK (date(race_date) IS race_date),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    archived_at TEXT
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', archived_at) IS archived_at),
    CHECK (race_date >= start_date),
    CHECK ((status = 'active' AND archived_at IS NULL)
        OR (status = 'archived' AND archived_at IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX macro_plan_single_active
    ON macro_plan (status) WHERE status = 'active';

CREATE TABLE macro_revision (
    id INTEGER PRIMARY KEY,
    macro_plan_id INTEGER NOT NULL REFERENCES macro_plan (id),
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    UNIQUE (macro_plan_id, revision_number)
) STRICT;

CREATE TABLE weekly_plan (
    id INTEGER PRIMARY KEY,
    macro_plan_id INTEGER NOT NULL REFERENCES macro_plan (id),
    week_start TEXT NOT NULL CHECK (date(week_start) IS week_start),
    week_end TEXT NOT NULL CHECK (date(week_end) IS week_end),
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json)),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    updated_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
    CHECK (julianday(week_end) - julianday(week_start) BETWEEN 0 AND 6),
    CHECK (updated_at >= created_at),
    UNIQUE (macro_plan_id, week_start)
) STRICT;

CREATE TABLE workout (
    id INTEGER PRIMARY KEY,
    weekly_plan_id INTEGER NOT NULL REFERENCES weekly_plan (id),
    schedule_date TEXT NOT NULL CHECK (date(schedule_date) IS schedule_date),
    name TEXT NOT NULL
        CHECK (length(name) BETWEEN 1 AND 15 AND name NOT GLOB '*[^ -~]*'),
    garmin_workout_id TEXT NOT NULL UNIQUE CHECK (length(garmin_workout_id) > 0),
    garmin_schedule_id TEXT UNIQUE CHECK (length(garmin_schedule_id) > 0),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    UNIQUE (weekly_plan_id, name)
) STRICT;

CREATE TABLE activity (
    garmin_activity_id TEXT PRIMARY KEY NOT NULL
        CHECK (length(garmin_activity_id) > 0),
    sport TEXT NOT NULL CHECK (sport IN ('running', 'swimming')),
    activity_type TEXT NOT NULL CHECK (length(activity_type) > 0),
    start_time TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', start_time) IS start_time),
    activity_date TEXT NOT NULL CHECK (date(activity_date) IS activity_date),
    distance_m REAL CHECK (distance_m >= 0),
    duration_s REAL CHECK (duration_s >= 0),
    avg_hr REAL CHECK (avg_hr >= 0),
    max_hr REAL CHECK (max_hr >= 0),
    avg_speed_mps REAL CHECK (avg_speed_mps >= 0),
    avg_cadence_spm REAL CHECK (avg_cadence_spm >= 0),
    splits_json TEXT CHECK (json_valid(splits_json)),
    raw_json TEXT NOT NULL
        CHECK (CASE WHEN json_valid(raw_json) THEN json_type(raw_json) = 'object' ELSE 0 END),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at)
) STRICT;

CREATE INDEX activity_activity_date ON activity (activity_date);

CREATE TABLE wellness (
    wellness_date TEXT PRIMARY KEY NOT NULL
        CHECK (date(wellness_date) IS wellness_date),
    resting_hr REAL CHECK (resting_hr >= 0),
    sleep_seconds REAL CHECK (sleep_seconds >= 0),
    body_battery_high REAL CHECK (body_battery_high >= 0),
    body_battery_low REAL CHECK (body_battery_low >= 0),
    stress_avg REAL CHECK (stress_avg >= 0),
    hrv_last_night_ms REAL CHECK (hrv_last_night_ms >= 0),
    vo2max REAL CHECK (vo2max >= 0),
    raw_json TEXT NOT NULL
        CHECK (CASE WHEN json_valid(raw_json) THEN json_type(raw_json) = 'object' ELSE 0 END),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    updated_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
    CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE checkin (
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL CHECK (date(week_start) IS week_start),
    fatigue INTEGER NOT NULL CHECK (fatigue BETWEEN 1 AND 10),
    has_pain INTEGER NOT NULL CHECK (has_pain IN (0, 1)),
    pain_location TEXT,
    pain_level INTEGER,
    free_text TEXT CHECK (length(trim(free_text)) > 0),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    CHECK (
        (has_pain = 0 AND pain_location IS NULL AND pain_level IS NULL)
        OR (
            has_pain = 1
            AND pain_location IS NOT NULL
            AND pain_level IS NOT NULL
            AND length(trim(pain_location)) > 0
            AND pain_level BETWEEN 1 AND 10
        )
    )
) STRICT;

CREATE INDEX checkin_week_start ON checkin (week_start);

CREATE TABLE message (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL CHECK (length(content) > 0),
    weekly_plan_id INTEGER REFERENCES weekly_plan (id),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at)
) STRICT;

CREATE TABLE coach_summary (
    id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    created_at TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at)
) STRICT;
