# Onboarding

Onboarding is a chat with the coach in the terminal. The coach asks what it needs:
the target race, the running history, injuries and the days you can run. At the end
the app stores the athlete profile and the macro plan to the race, then pushes week 1
to the Garmin calendar at once.

## Before the first run

- Turn off any Garmin Coach plan and any Adaptive Training Plan in Garmin Connect.
  See [garmin-push.md](garmin-push.md).
- Run `uv run training-season-pull` first. The coach then sees the Garmin history.
- Sign in to Garmin once with `uv run training-season-garmin`. See
  [garmin-auth.md](garmin-auth.md).

## Environment

| Variable | Use |
| --- | --- |
| `TZ` | IANA timezone of the athlete. "Today" is the date in this zone |
| `DATABASE_PATH` | Absolute path to the SQLite file |
| `GARMIN_TOKEN_DIR` | Absolute path to the Garmin token directory |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | Only when the token cache needs a new login |
| `LLM_PROVIDER` | `openai_compatible` (default) or `anthropic` |
| `ANTHROPIC_API_KEY`, `CLAUDE_CHAT_MODEL`, `CLAUDE_PLAN_MODEL`, `CLAUDE_CHAT_EFFORT`, `CLAUDE_PLAN_EFFORT` | With `LLM_PROVIDER=anthropic` |
| `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_CHAT_MODEL`, `OPENAI_COMPATIBLE_PLAN_MODEL` | With the default provider |
| `COACH_INSTRUCTION_PATH` | Optional. See [coach-instruction.md](coach-instruction.md) |

The interview turns use the chat model. The macro plan and week 1 use the plan
model. Defaults and limits are in [llm.md](llm.md).

## Commands

```sh
uv run training-season-onboarding chat
uv run training-season-onboarding reopen
uv run training-season-onboarding week
uv run training-season-onboarding discard
uv run training-season-onboarding history
uv run training-season-onboarding history 1
```

### chat

Starts the first onboarding, or resumes the open one. The coach speaks first. Type
your answer after `Você: `. A blank line is ignored.

Press Ctrl-D or type `/sair` to pause. The session and its messages stay in SQLite,
and the next `chat` continues from the last answer.

Control characters in your answer and in the coach reply are removed, line breaks
stay. Two size limits protect the model context and the cost of each turn:

- An answer over 4000 characters is refused before any model call. Nothing is
  stored. Send a shorter answer.
- When the stored messages and the new answer pass 100 000 characters, the turn is
  refused before any model call and `chat` stops with exit code `2`. Run `discard`
  and start again.

When the coach has every answer and you confirm, the app:

1. Stores the profile, the macro plan and macro revision 1 with its reason, in one
   transaction.
2. Asks the plan model for week 1 and pushes it to Garmin.

`chat` with an active macro plan and no open session stops with exit code `2`. Use
`reopen` to change the goal.

### Week 1

Week 1 runs from today to the coming Sunday, 1 to 7 days. The weekly cycle plans
Monday to Sunday after it. The push removes and replaces only this app's workouts
from today to that Sunday. Workouts already on later days stay.

### reopen

Starts a new onboarding session while the current macro plan stays active. Tell the
coach what changed and why. At completion the app archives the current macro plan,
stores the new one from today, and pushes week 1 again. The new macro plan's
revision 1 holds your reason.

A paused reopen changes nothing. The current plan and the calendar stay until the
reopen completes.

### week

Generates and pushes week 1 again for the active macro plan. Use it after an MFA
stop or after a rejected week.

### discard

Closes the open session, from `chat` or `reopen`, with no macro plan. Its messages
stay in SQLite. The profile, the active macro plan and the calendar do not change.
The next `chat` or `reopen` starts a new session. With no open session, `discard`
stops with exit code `2`.

### history

`history` lists every macro plan, archived and active, with its dates and its
revisions with their reasons. `history <id>` prints the stored JSON of one macro
plan. Archived plans stay readable.

## Web onboarding

The [chat page](web.md) runs the same onboarding as the commands. It uses the same
session, the same limits and the same coach turns. The page and the commands share
one database, so you can start in one and continue in the other.

- With no active macro plan, the page shows the intro and `Começar`. The intro has
  no `/sair` hint. To pause, leave the page. The messages stay in SQLite and the
  page shows them again.
- `Revisar objetivo` is `reopen`. The current macro plan stays active until the new
  one is ready. A request for a first onboarding with an active plan gets 409 `Já
  existe um plano ativo. Use Revisar objetivo para mudar o objetivo.` A request to
  reopen with no active plan gets 409 `Nenhum plano ativo. Comece o onboarding.`
- `Descartar sessão` is `discard`. It shows `Sessão de onboarding descartada. As
  mensagens ficam salvas.` With no open session it gets 409 `Nenhuma sessão de
  onboarding aberta.`
- A model failure stores nothing for that turn. The page shows `Não consegui falar
  com o treinador agora. Sua mensagem não foi salva. Envie de novo.` and keeps your
  text for a new send.
- A session over the size limit shows `Esta sessão de onboarding ficou longa demais
  para continuar. Descarte a sessão para começar de novo.`
- When the coach completes the onboarding, the page asks for week 1 once, with
  `POST /api/week-one`. It never asks by itself when it loads. In mode
  `week_one_pending` it shows `Enviar semana 1`.

Only the web stores the week-1 outcome. `POST /api/week-one` writes the outcome text
(pushed, partial, empty, rejected, mfa_required or failed) as one coach `message` row, so the chat
history keeps it also when the request drops. The `week` command stores nothing and
only prints. A rejected week shows the count of validation errors, never a model
value.

## Garmin MFA

When the web page stops for MFA, the MFA panel shows and you enter the code there.
See [garmin-auth.md](garmin-auth.md#code-in-the-browser). The rest of this section
is for the commands.

When Garmin asks for MFA during the week-1 push, the command stops with exit code
`3`. The profile and the macro plan are already stored. Finish the login with
`uv run training-season-garmin`, then run `uv run training-season-onboarding week`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Done, or the chat was paused |
| `1` | Partial Garmin push, or a Garmin, model, SQLite or migration failure |
| `2` | Bad arguments, configuration, or a command that does not fit the plan state |
| `3` | Garmin requires MFA, or the model returned an invalid week twice |

A model failure during the chat stops with exit code `1`. Nothing is stored for that
turn, and the next `chat` resumes the session.

## Privacy

Log lines hold the session id, the turn status and the macro plan id. They never
hold the profile, the messages or the model replies.
