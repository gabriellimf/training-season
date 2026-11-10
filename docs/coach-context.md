# Coach context

The coach context is the text the coach model reads before it plans a week. It is
built from SQLite only. It makes no Garmin call and no model call, and it writes
nothing.

The weekly cycle calls two functions:

- `assemble_coach_context(connection, target_week_start)` in
  `training_season.coach_context` reads the context.
- `update_athlete_summary(connection, adapter, system=..., context=...)` in
  `training_season.athlete_summary` asks the model to rewrite the athlete summary
  and stores it.

The two commands below run the same functions by hand.

## Sections

The text has six sections, always in this order, with Portuguese headings:

1. `## Plano macro`: the active macro plan, its start date, race date and plan JSON.
   Without an active macro: `Sem plano macro ativo.`
2. `## Resumo do atleta`: the newest `coach_summary` row. Before the first one:
   `Ainda não há resumo.`
3. `## Últimas quatro semanas`: the four weeks before the target week in full
   detail. Each week has its plan (or `sem plano`), every activity with its laps,
   and every wellness day. The plan of a week is the active macro's weekly plan
   whose start date falls in that week; of two, the later one. An activity shows
   at most 20 laps, then `+N voltas`. Swims are marked
   `natação, só leitura, não planejar`.
4. `## Semanas anteriores`: one line of totals per older week, back to the macro
   start date. Run count, km and time, then swim count and km.
5. `## Perfil`: the profile JSON. Before onboarding: `Perfil ainda não preenchido.`
6. `## Check-in`: the check-in with the latest week before the target week, the
   number of weeks in a row with no check-in, and the weeks to the race. The pain
   location and the free text are each on one line, without control characters.
   The location is cut after 200 characters and the free text after 2000. A cut
   text ends with ` […] (texto cortado)`.

JSON values are canonical: sorted keys, no spaces. The same database gives the same
text, byte for byte. Raw Garmin JSON never goes into the context.

## Week windows

The input is the target week: the first day of the week the coach will plan. Windows
are 7-day spans counted back from it. Window -1 is the 7 days before the target,
window -2 the 7 days before that, and so on. Dates are training dates in the `TZ`
timezone, as stored.

- Windows -1 to -4 are the recent weeks, always four, also before the macro start.
- Windows -5 and older are the older weeks. They stop at the window that holds the
  macro start date, and that window counts only the days from the macro start on.
  With no active macro there are no older weeks.

## Why the cost stays flat

The context must cost about the same at week 30 as at week 5. Three rules keep it
so:

- Full detail is only for four weeks. That part has the same size every week for the
  same training.
- Older weeks are one short line each, and they stop at the macro start. History
  before the macro lives in the athlete summary.
- The athlete summary is rewritten each week, not appended to. It is at most 4000
  characters.

A test builds a 30-week block and checks that the text at week 30 is at most 1.2
times the text at week 5.

## Show the context

```sh
uv run training-season-context show 2026-11-02
```

The command prints the context on stdout and `Contexto: <N> caracteres` on stderr.
It needs `DATABASE_PATH`, as an absolute path. It writes nothing to the database
except pending migrations.

## Rewrite the athlete summary

```sh
uv run training-season-context summary 2026-11-02
```

The command builds the context for that week and sends it to the model once, with
the `chat` job. The system text is the coach instruction plus the summary contract.
The model must answer with plain text of 1 to 4000 characters.

A good answer is stored as a new `coach_summary` row. The command never changes or
deletes old rows. The command prints `Resumo salvo: id <N>`, then the summary.

An answer is rejected when it is empty, longer than 4000 characters, holds a control
character other than newline and tab, or has a line that starts with `#`. Nothing is
stored and the previous summary stays. There is no retry.

To undo a bad summary that was stored, delete its row by id. The previous row is
then the newest, and the next context uses it:

```sh
sqlite3 "$DATABASE_PATH" "SELECT id, created_at, length(summary) FROM coach_summary ORDER BY id DESC LIMIT 5;"
sqlite3 "$DATABASE_PATH" "DELETE FROM coach_summary WHERE id = <id>;"
```

The command needs `DATABASE_PATH`, `TZ`, and the LLM settings (`LLM_PROVIDER` and the
API key of that provider).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Done |
| `1` | SQLite, migration or model provider failure |
| `2` | Bad date argument or bad configuration |
| `3` | The model's summary was rejected (`summary` only) |

## Logs

Logs hold counts, dates, lengths, row ids and the model request id. They never hold
the context text, the summary text, check-in text, profile values or health values.
