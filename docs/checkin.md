# Check-in

The check-in tells the coach how you feel. Garmin cannot measure this. It is
optional, and you can run it on any day. The next weekly plan reads it.

The command asks three fixed questions. When the answers are stored, you can talk
with the coach about the week. Type `/sair` or press Ctrl-D to end.

## Environment

| Variable | Use |
| --- | --- |
| `TZ` | IANA timezone of the athlete. "Today" is the date in this zone |
| `DATABASE_PATH` | Absolute path to the SQLite file |
| `LLM_PROVIDER` | `openai_compatible` (default) or `anthropic` |
| `ANTHROPIC_API_KEY`, `CLAUDE_CHAT_MODEL`, `CLAUDE_CHAT_EFFORT` | With `LLM_PROVIDER=anthropic` |
| `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_CHAT_MODEL` | With the default provider |
| `COACH_INSTRUCTION_PATH` | Optional. See [coach-instruction.md](coach-instruction.md) |

The conversation uses the chat model: Claude Sonnet with `LLM_PROVIDER=anthropic`.
Defaults are in [llm.md](llm.md). The check-in needs no Garmin setting.

## Command

```sh
uv run training-season-checkin
docker compose run --rm app training-season-checkin
```

The command takes no arguments. `--help` prints the usage line.

## The questions

1. `Como está sua fadiga hoje, de 1 a 10?`
2. `Algo está doendo? (s/n)`
3. Only after `s`: `Onde dói?` and then `De 1 a 10, quanto dói?`

Type the answer after `Você: `. Fatigue and pain level are whole numbers from 1 to
10. The pain answer is `s` or `n`. The location is 1 to 200 characters on one
line. A bad answer prints a hint, and the same question comes again.

`/sair` or Ctrl-D before the last answer ends the command with exit code `0`.
Nothing is stored and no model is called.

## The conversation

After the last answer the app prints `Check-in salvo.` and the conversation starts.
A blank line is ignored. Each message goes to the coach with:

- the coach instruction as the system text,
- the coach context for the next Monday, built once when the answers are stored.
  Its `## Check-in` section already holds your answers,
- the messages and replies of this run.

The app stores your message before the model call. Control characters in the coach
reply are removed before it prints, line breaks stay.

Two size limits protect the model context and the cost of each turn:

- A message over 4000 characters is refused. It is not stored and not sent. Send a
  shorter message.
- When the messages and replies of this run and the new message pass 100 000
  characters, the message is refused and the command stops with exit code `2`.
  The answers and the earlier messages stay stored.

## What is stored

One new `checkin` row for each run that reaches the last answer:

- `week_start`: the Monday of the local week of the day you answer.
- `fatigue`, `has_pain`, `pain_location`, `pain_level`: your answers.
- `free_text`: your conversation messages, one per line, in order. Empty with no
  message.
- `created_at`: the UTC time of the run.

The coach replies are not stored. A second check-in in the same week is a new row.
The newest row of the latest week is the one the coach reads.

## Which plan reads it

The weekly cycle runs on Sunday at 17:00 and plans the Monday after it. It reads the
latest check-in with a week before that Monday. A check-in from Monday to Sunday
before 17:00 reaches the plan of that Sunday.

Example: a check-in on Wednesday 2026-11-18 has week 2026-11-16. The run on Sunday
2026-11-22 plans the week of 2026-11-23 and reads it.

A check-in on Sunday after 17:00 still has the week of that Monday, but the run of
that day is already done. It reaches the plan one week later, and that plan also
says `Sem check-in há 1 semana.` A new check-in in the next week replaces
it.

With no check-in, the context counts the weeks in a row with no check-in, back to
the macro start. See [coach-context.md](coach-context.md).

To see what the next plan will read, show the context for the next Monday:

```sh
uv run training-season-context show 2026-11-23
```

The `## Check-in` section prints the answers and `Texto livre: ...`. The free text
is one line there and is cut after 2000 characters.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Done, ended with `/sair` or Ctrl-D, or `--help` |
| `1` | Model, SQLite or migration failure, or a stored profile that is not valid |
| `2` | Bad arguments, configuration, or the conversation is too long |

A configuration error (`TZ`, `DATABASE_PATH`, the LLM settings, the coach
instruction) stops before the first question. Nothing is stored.

A model failure during the conversation prints `Não consegui falar com o treinador
agora. Suas respostas e mensagens estão salvas.` and stops with exit code `1`. The
answers and the message that was sent stay stored. A reply that is empty after
control characters are removed is a model failure too.

## Privacy

A model failure logs one check-in line at WARNING:
`Check-in reply failed error_type=<name>`. The name is the error class, or
`EmptyReply` for an empty reply. For an adapter error, the adapter first logs its
own failure line (`Claude call failed: ...` or `OpenAI-compatible call failed: ...`) with the
job, the model, the error class, the HTTP status, the provider error type, the
request id and the duration. For a refusal or a stop error (Claude `stop_reason`
`refusal` or `max_tokens`, OpenAI-compatible `finish_reason` `content_filter` or
`length`), the adapter logs its normal response line instead, then raises. That line
holds the job, the model, the stop or finish reason, the input, output, cache read
and cache write token counts, the cost, the duration and the request id. Log lines
never hold the prompt, the answers, the messages, the context, the model replies or
the API key.
