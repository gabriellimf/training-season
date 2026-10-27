# Garmin data pull

The pull stores running and swimming activities and daily wellness data in SQLite.
It does not pull Training Status, Training Load, or workout details.

## Run

Run the first pull by hand. A long history can take many requests and holds the
Garmin token lock for the full operation.

```sh
uv run training-season-pull
```

In Docker Compose:

```sh
docker compose run --rm app training-season-pull
```

The command prints the requested date windows and record counts. Exit code `0` means
success. Exit code `3` means Garmin requires MFA. Exit code `2` means configuration
is invalid. Exit code `1` means Garmin, SQLite, or a migration failed.

`GARMIN_HISTORY_START` optionally limits the first pull. Set it to a strict
`YYYY-MM-DD` date. It applies only while each table is empty. Later pulls use each
table's stored cursor, even when this variable is set. A future date is rejected
before any Garmin data call.

Without this setting, the first activity pull starts one day before the oldest
Garmin activity. The first wellness pull starts at the earliest stored activity date.
If there are no activities, wellness starts on the current training date. The first
pull can therefore make many per-day wellness requests.

## Stored data

Activities store the raw type key, start time, training date, distance, duration,
heart rates, average speed, running cadence, and the full lap response. The schema's
units are assumed: metres, seconds, metres per second, beats per minute, and steps
per minute. The library source does not state these units. The owner must confirm
them during the first real pull.

Wellness stores daily resting heart rate, sleep duration, Body Battery high and low,
stress, HRV, and VO2max. The daily summary, HRV response, and max-metrics response
remain in `raw_json` for each stored day. The VO2max path is based on public
third-party code, not a response captured from this account. Unsupported response
shapes leave the column `NULL`.

The installed `python-garminconnect` 0.3.20 source documents the daily summary
fields, but does not verify the activity-type list shape or the maximum supported
daily-summary span. The pull fails before activity writes if the activity-type list
shape differs from the expected list. The owner must confirm these shapes during the
first real pull.

Each day needs separate HRV and max-metrics requests. Daily summaries use one request
per window of at most 28 days. Each new supported activity adds one laps request.
Full-history cost grows with the number of days and new activities. Use
`GARMIN_HISTORY_START` to bound the first pull.

Garmin assigns wellness dates in the profile timezone. A wellness date matches an
activity training date only when the Garmin profile and watch use the training
timezone. The pull does not adjust for a mismatch.

## Deltas and recovery

Each later pull starts 14 days before the latest stored date for that table. Activity
IDs use `ON CONFLICT DO NOTHING`, so activities already stored are not fetched again
for their laps. Garmin edits to stored activities are not applied. Wellness rows
inside the overlap are upserted, and their original creation timestamp is kept.

The overlap lets a rerun resume after a failed request. Activity records and their
lap response commit together. Wellness records commit one completed date window at a
time. No write transaction remains open during a Garmin request. A failure during a
window leaves that window unchanged. A failure after earlier commits leaves those
complete rows in place.

An activity synced more than 14 days late can be missed. The pull skips multisport
activities rather than splitting them into separate legs. It counts skipped and
invalid activities without printing activity values or names.

The pull holds the Garmin token lock for the full run. Login and MFA operations wait
for the lock and can time out while a long pull runs. Do not schedule the first
history pull as an unattended cron job. A rate limit stops the run. Run the command
again later; the stored cursors and overlap let it resume.
