# Weekly cycle

One command plans the next week and pushes it to the Garmin calendar. Nothing waits
for approval. Every run ends with one coach message in the chat: the week, or the
step that failed and why.

## Before the first run

- Complete onboarding. The cycle needs an active macro plan. See
  [onboarding.md](onboarding.md).
- Sign in to Garmin once with `uv run training-season-garmin`. See
  [garmin-auth.md](garmin-auth.md).

## Environment

| Variable | Use |
| --- | --- |
| `TZ` | IANA timezone of the athlete. "Today" is the date in this zone |
| `DATABASE_PATH` | Absolute path to the SQLite file |
| `GARMIN_TOKEN_DIR` | Absolute path to the Garmin token directory |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | Only when the token cache needs a new login |
| `GARMIN_HISTORY_START` | Optional. Used only by the first pull. See [garmin-pull.md](garmin-pull.md) |
| `LLM_PROVIDER` | `openai_compatible` (default) or `anthropic` |
| `ANTHROPIC_API_KEY`, `CLAUDE_CHAT_MODEL`, `CLAUDE_PLAN_MODEL`, `CLAUDE_CHAT_EFFORT`, `CLAUDE_PLAN_EFFORT` | With `LLM_PROVIDER=anthropic` |
| `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_CHAT_MODEL`, `OPENAI_COMPATIBLE_PLAN_MODEL` | With the default provider |
| `COACH_INSTRUCTION_PATH` | Optional. See [coach-instruction.md](coach-instruction.md) |

The week uses the plan model. The athlete summary uses the chat model. Defaults and
limits are in [llm.md](llm.md).

## Command

```sh
uv run training-season-weekly
uv run training-season-weekly --scheduled
```

Both forms run the same cycle. `--scheduled` only records the run as started by the
scheduler; without it the run is recorded as manual. The cycle plans the Monday after
today through the Sunday after that Monday. A run on Sunday plans the next day on.

Only one run goes at a time. A run holds an exclusive lock on `weekly.lock` in the
directory of `DATABASE_PATH`. A second run that finds the lock held does not wait: it
stores a chat message and exits `4`. The lock goes away with the process that holds
it, so a crashed or killed run never blocks the next one.

## Steps

The steps run in this order. The first step that fails ends the run, and no later
step runs. Steps that already ran keep their writes: pulled rows stay, and days
that landed on Garmin stay.

| Step | What it does |
| --- | --- |
| 1 | Pull Garmin activities and wellness since the last pull |
| 2 | Assemble the coach context for the target Monday. Step 7 reads the same context |
| 3 | Ask the plan model for the week |
| 4 and 5 | Validate the week and its workout names. An invalid reply gets one retry with the errors |
| 6 | Read last week's running distance, store the week under the active macro plan and push it to Garmin. Only this app's workouts in that week are replaced |
| 7 | Ask the chat model to rewrite the state-of-the-athlete summary, and store it as a new row |
| 8 | Store the chat message |

A failure in steps 4 and 5 is reported as step 4.

## Chat message

Each run that opens the database stores one `message` row with `role = 'assistant'`
for itself and prints the same text on stdout. A run that first closes runs a hard
kill left open stores one more message per closed run (see [Run record](#run-record)).
`weekly_plan_id` points to the week when step 6 stored it, else it is empty.

After a full run the message lists one line per workout day, the planned volume,
the easy and hard split, then the Garmin push result:

```text
Treinos da semana de 16/11 a 22/11:
- ter 17/11 Leve
- qui 19/11 Tiros
Volume planejado: 13,2 km (-6,8 km contra 20,0 km corridos na semana passada).
Leve: 8,0 km · Forte: 5,2 km.
Semana de 16/11 enviada ao Garmin. Agendados: 17/11, 19/11.
```

- Planned distance of a step: `duration.meters`, or `duration.seconds` times the
  middle of its `pace_mps` range. A `repeat` counts `count` times its steps. The week
  is the sum of its workouts.
- Easy and hard metres are each rounded once to whole 100 m. The split line shows
  those values, and the planned volume is their sum, so the shown numbers add up.
- Last week: the `distance_m` sum of the `running` activities of the Monday to
  Sunday before the planned week. Swims do not count. With no run it is `0,0`. The
  delta sign is `+` or `-`, and `±0,0` when the two are equal.
- A workout is hard (`Forte`) when it has an `interval` step, also inside a
  `repeat`. Else it is easy (`Leve`).

After a failed step the message names the step and the cause:

```text
Ciclo semanal parou na etapa 6 (envio ao Garmin): <cause>
As etapas seguintes não rodaram.
```

The cause is a fixed Portuguese text per error type, the count of validation errors
of a rejected week, or the Garmin push result. When the push at step 6 returns a
failed day, the message starts with the planned week (day, volume and split
lines), and the push result is the cause. This also holds when no day
landed: the week block is what the coach planned, the push result says what
reached Garmin. A failure at step 7 starts with the full week text of a run that
succeeds, because the week is already on Garmin. Error text from Garmin or the
model, and values from a model reply, never reach the chat. The validation errors
go only to the model, in the retry turn.

If the chat message itself cannot be stored, the text is still printed on stdout,
the error goes to stderr, the run ends `failed` with no message, and the command
stops with exit code `1`.

When the cycle does not reach its own message, the run stores a fixed text:

| Case | Message |
| --- | --- |
| A check before step 1 fails | `Ciclo semanal não rodou: <cause>` |
| Another run holds the lock | `Ciclo semanal não rodou: outra execução já estava em andamento.` |
| The lock file cannot be used | `Ciclo semanal não rodou: a trava do ciclo semanal não pode ser usada.` |
| An unexpected error | `Ciclo semanal parou por um erro inesperado.`, then `As etapas seguintes não rodaram.` and `Rode training-season-weekly para tentar de novo.` |
| SIGTERM or Ctrl-C during the cycle, or a run left open | `A execução do ciclo semanal iniciada em DD/MM às HH:MM foi interrompida antes do fim. Rode training-season-weekly para tentar de novo.` |

The causes before step 1 are fixed texts: the model provider configuration, the coach
instruction, the `TZ` value, the Garmin configuration, no active macro plan, or a
database failure. The start time of an interrupted run is shown in `TZ`. When `TZ` is
not valid, it is shown in UTC with a `UTC` suffix.

## Run record

Every run that opens the database writes one `cycle_run` row. `--close-open-runs`
writes none (see Scheduler).

| Column | Value |
| --- | --- |
| `trigger` | `scheduled` with `--scheduled`, else `manual` |
| `started_at`, `ended_at` | UTC timestamps. `ended_at` is empty while the run goes |
| `outcome` | See below. Empty while the run goes |
| `exit_code` | The exit code of the command |
| `message_id` | The chat message of the run, empty when the message could not be stored |

| Outcome | When | Exit code |
| --- | --- | --- |
| `done` | The cycle ran to the end | `0` |
| `failed` | A step failed, a check before step 1 failed, the lock file cannot be used, or the chat message could not be stored | `1`, `2` or `3` |
| `crashed` | An unexpected error stopped the cycle | `1` |
| `interrupted` | SIGTERM or SIGINT (Ctrl-C) during the cycle, or a run that a hard kill left open | `1` |
| `refused` | Another run held the lock. `started_at` equals `ended_at` | `4` |

A hard kill (`SIGKILL`, power loss) leaves the row open. The next run that takes the
lock closes every open row as `interrupted` and stores one message per row before it
starts its own cycle. A refused run does not touch open rows, because the run that
holds the lock may still be going.

SIGTERM and SIGINT (Ctrl-C) stop the cycle at once. The run ends `interrupted` with
its message and exit code `1`. The cycle message and the end of the row are written
together, so a run has exactly one message. While the command writes its rows, before
the cycle starts and after it ends, the two signals wait:

- A signal before the cycle starts stops the run when the cycle starts.
- A signal after the cycle ends does not change the stored row. When the row is
  written, the signal has its default effect: the process stops.

On an unexpected error stderr gets only the error type name and the traceback frames,
never the error text.

## Scheduler

The `scheduler` service in `docker-compose.yml` runs [supercronic](https://github.com/aptible/supercronic)
with `deploy/crontab`:

```
0 17 * * 0 training-season-weekly --scheduled
```

```sh
docker compose up -d scheduler
```

- The run is every Sunday at 17:00 in `TZ` (`America/Sao_Paulo` in Compose). The
  crontab has no `CRON_TZ` line: supercronic uses the `TZ` of the container.
- One attempt, no retry. A failed run leaves its chat message and its `cycle_run`
  row. The next try is the next Sunday, or a manual run.
- supercronic does not start a second copy of a job that still runs. The run lock
  also stops two runs at once.
- A stop (`docker compose stop scheduler`) or a reboot during a run kills the job.
  supercronic does not pass SIGTERM to the job. It waits, and after the grace period
  Docker kills the container. When the scheduler starts again, it closes that run as
  `interrupted` and stores its message.
- At each start the scheduler runs `training-season-weekly --close-open-runs`, then
  supercronic. That command runs no cycle and writes no `cycle_run` row of its own. It
  closes every open row as `interrupted` with its message and exits `0`. When another
  run holds the lock, it writes one line on stderr and exits `0`. A database or
  configuration error goes to stderr with exit `1`, and the scheduler still starts.
- Keep `/data` on the named volume `data`. The run lock uses `flock`, and `flock` does
  not lock across a Docker Desktop bind mount and a process on the host.
- America/Sao_Paulo has had no daylight saving change since 2019-02-17 (tzdata 2026f),
  so the local time does not shift. A future rule arrives with the `tzdata` package
  of the image, after a rebuild.

Manual trigger, one-shot, no scheduler needed:

```sh
docker compose run --rm app training-season-weekly
```

Logs. Job output and the exit status of each run go to the container log, one line
per output line, with a timestamp and the channel (`stdout` or `stderr`):

```sh
docker compose logs scheduler
```

Run record. The last ten runs, with trigger, times, outcome, exit code and message:

```sh
docker compose run --rm app python -c "import os, sqlite3; db = sqlite3.connect(os.environ['DATABASE_PATH']); [print(row) for row in db.execute('SELECT id, trigger, started_at, ended_at, outcome, exit_code, message_id FROM cycle_run ORDER BY id DESC LIMIT 10')]"
```

## Garmin MFA

When Garmin asks for MFA, the run stops with exit code `3` and stores the failure
message. Finish the login with `uv run training-season-garmin`, then run
`uv run training-season-weekly` again.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Done |
| `1` | Partial or failed Garmin push, a Garmin, model, SQLite or migration failure, an unexpected error, SIGTERM or SIGINT, a lock file that cannot be used, or a chat message that cannot be stored |
| `2` | Bad arguments or configuration, or no active macro plan |
| `3` | Garmin requires MFA, the model returned an invalid week twice, or the new summary broke its format |
| `4` | Another run holds the lock |

The checks before step 1 run after the database opens and stop before any Garmin or
model call. They store the `Ciclo semanal não rodou` message and a `failed` run. A
bad configuration exits `2`, a database failure exits `1`. Two cases have stderr only,
because nothing can be stored: a bad `DATABASE_PATH` (exit `2`) and a migration or
SQLite failure while the database opens (exit `1`). A Garmin configuration error found
inside a step (no token and no credentials, or a `GARMIN_HISTORY_START` after today)
also exits `2`, and stores the failure message.

## Privacy

Logger lines on stderr hold step numbers, dates, counts and request ids. They never
hold the prompt, the model replies, the summary, the check-in or health values.

The job also prints each chat message on stdout, so `docker compose logs scheduler`
keeps a copy of it: the planned week, its volume and last week's run distance.
