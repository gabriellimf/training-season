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
```

The command takes no arguments. It plans the Monday after today through the Sunday
after that Monday. A run on Sunday plans the next day on.

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

Each run stores exactly one `message` row with `role = 'assistant'` and prints the
same text on stdout. `weekly_plan_id` points to the week when step 6 stored it, else
it is empty.

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
the error goes to stderr and the command stops with exit code `1`.

## Garmin MFA

When Garmin asks for MFA, the run stops with exit code `3` and stores the failure
message. Finish the login with `uv run training-season-garmin`, then run
`uv run training-season-weekly` again.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Done |
| `1` | Partial or failed Garmin push, or a Garmin, model, SQLite or migration failure |
| `2` | Bad arguments or configuration, or no active macro plan |
| `3` | Garmin requires MFA, the model returned an invalid week twice, or the new summary broke its format |

Exit code `2` from the checks before step 1 stops before any Garmin or model call
and stores no chat message. A Garmin configuration error found inside a step (no
token and no credentials, or a `GARMIN_HISTORY_START` after today) also exits `2`,
and stores the failure message.

## Privacy

Log lines hold step numbers, dates, counts and request ids. They never hold the
prompt, the model replies, the summary, the check-in or health values.
