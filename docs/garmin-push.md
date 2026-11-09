# Garmin week push

The push puts one week of running workouts on the Garmin Connect calendar. Each
workout is uploaded to the Garmin workout library, then scheduled on its date.

## Before the first push

Turn off any Garmin Coach plan and any Adaptive Training Plan in Garmin Connect.
Those plans add their own calendar entries. The push does not remove them, and the
watch then shows both sets of workouts.

## Run

```sh
uv run training-season-push week.json
```

In Docker Compose, mount the file into the container:

```sh
docker compose run --rm -v "$PWD/week.json:/tmp/week.json:ro" app \
  training-season-push /tmp/week.json
```

The command needs `DATABASE_PATH` and `GARMIN_TOKEN_DIR`, as an absolute path. It
also needs one active macro plan in SQLite. Without one, it stops before any Garmin
call.

The command prints the chat text in Portuguese. Exit code `0` means every workout
landed. Exit code `1` means a partial push, or a Garmin, SQLite or migration
failure. Exit code `2` means the input file or the configuration is invalid. Exit
code `3` means Garmin requires MFA.

## Input file

```json
{
  "week_start": "2027-01-04",
  "workouts": [
    {"date": "2027-01-04", "workout": {"name": "04/01 Leve 6k", "steps": [...]}}
  ]
}
```

- `week_start` and every `date` are strict `YYYY-MM-DD` dates. They are calendar
  dates in `America/Sao_Paulo`. The push sends them as is, and never derives them
  from a timestamp.
- Every `date` is in `week_start` to `week_start` + 6 days. One workout per date.
- `workout` follows the workout schema of `training-season-workout validate`.
- The workout name starts with the date as `DD/MM `, as `training-season-workout
  name` builds it. A name with another date fails.
- `workouts` can be empty. An empty week removes the workouts this app pushed on
  its 7 days.

An invalid file stops the command before any database write or Garmin call.

## Replacement rule

A push replaces the week. It never merges. The week is the 7 days from
`week_start`. Garmin `schedule_workout` adds one calendar entry and does not
remove the old ones, so the push does the removal:

1. For each workout this app stored on those 7 days, from any earlier push and
   any `week_start` or macro plan: remove the calendar entry, then
   delete the workout from the library, then delete the SQLite row. A Garmin
   "not found" means the item is already gone, and the push continues.
2. For each new workout, in date order: upload it and store the row, then schedule
   it and store the schedule id.

The push touches only the entries it created. Your own workouts on the same days
stay. Running the push again for the same week is safe: it removes the earlier
push first.

The Forerunner 165 manual says that a scheduled push overwrites the whole training
calendar on the watch. That is a device rule. The Garmin Connect API does not
overwrite the calendar.

A week of 7 workouts takes at most 28 workout requests, plus the login: 2 per
removed workout and 2 per new workout.

## Failures

The push stops at the first failed Garmin call. The chat text names the days that
landed, the failed day with the reason, and the days not tried. Each Garmin change
commits its SQLite change at once, so SQLite matches what Garmin holds. Run the
push again for the same week to finish it.

A SQLite error during a push, or a Garmin response lost on the network, can
leave one item on Garmin that no row tracks: a
workout in the library, or a calendar entry. The next push does not remove it.
`training-season-push show` finds it. Remove it in Garmin Connect.

Logs hold counts, dates and Garmin ids only. They never hold workout names, notes,
paces, Garmin response bodies or credentials.

## Read-back

```sh
uv run training-season-push show 2027-01-04
```

The command prints the Garmin calendar entries of the 7 days, one `date name` line
each, from one read-only request. Use it to check that a push landed and that a
second push left no duplicates. The list also shows entries this app did not
create.
