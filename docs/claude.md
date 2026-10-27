# Claude adapter

The adapter sends every model request through `training_season.claude`. It uses
`anthropic==2.3.0`. It uses the synchronous, non-streaming Messages API.

## Configuration

`ANTHROPIC_API_KEY` is required. Set it in the process environment or the Compose
environment file. Do not store it in the repository.

The defaults are:

| Job | Model | Effort |
| --- | --- | --- |
| Sunday plan | `claude-opus-5-5` | `medium` |
| Chat | `claude-sonnet-5-5` | `medium` |

Set `CLAUDE_PLAN_MODEL` or `CLAUDE_CHAT_MODEL` to change one model. Each change uses
one environment variable. Set `CLAUDE_PLAN_EFFORT` or `CLAUDE_CHAT_EFFORT` to one of
`low`, `medium`, `high`, `xhigh`, `max`, or `none`. `none` omits `output_config`.
The adapter sends the selected effort explicitly.

Run the local check before starting a consumer:

```sh
uv run training-season-claude check
docker compose run --rm app training-season-claude check
```

The check validates configuration. It does not create an SDK client or contact
Anthropic. It exits with code `2` and a clear message when the key is missing.
On success, it prints the plan model id followed by the chat model id.

Each consumer must call `claude_config_from_environment()` first. Do this before
Garmin configuration, database access, or any other work. Pass the result to
`ClaudeAdapter`.

The SDK also reads `ANTHROPIC_BASE_URL` and `ANTHROPIC_CUSTOM_HEADERS` from the
environment. An explicit `api_key` from `ANTHROPIC_API_KEY` takes precedence over
credential environment values.

## Requests and retries

Plan requests allow 32,000 output tokens and use a 900-second timeout. Chat requests
allow 16,000 output tokens and use a 600-second timeout. Both use a 5-second connect
timeout and `max_retries=2`, which permits up to three HTTP attempts.

The SDK retries transient status and connection errors. This includes 408, 409, 429,
5xx, 529, connection errors, and timeouts. These retries are within one adapter call.
They do not retry an entire scheduled job. The SDK honors `Retry-After` for up to
4,294,967 seconds per wait.

With three attempts and no server retry delay, a plan call can take about 45 minutes.
A chat call can take about 30 minutes. `#15` may revisit the chat timeout and retry
settings.

The adapter uses non-streaming calls. This keeps request retries inside the SDK
retry path. Streaming, async calls, tools, structured output, and prompt caching are
out of scope.

## Cost and logs

The estimate uses the response model and usage counts. Prices are USD per million
tokens. The values come from
<https://platform.claude.com/docs/en/about-claude/pricing>, read 2026-10-27.

| Model ID | Input | 5-minute cache write | 1-hour cache write | Cache read | Output |
| --- | ---: | ---: | ---: | ---: | ---: |
| `claude-opus-5-5` | 4 | 5 | 8 | 0.20 | 20 |
| `claude-sonnet-5-5` | 2 | 2.50 | 4 | 0.10 | 10 |

The adapter logs `unknown` cost for other response models, refusals, non-standard
service tiers, fast inference, non-global inference geography, or cache writes
without a cache TTL breakdown. It does not store usage in SQLite.

Each completed call logs the model, token counts, estimated cost, duration, and
request id. A failed call logs the model, adapter error type, HTTP status, API error
type, request id, and duration. It never logs job labels, effort, stop reasons,
prompts, turns, responses, thinking, refusal explanations, server messages, or API
keys.

The adapter sets the `anthropic`, `httpx2`, and `httpcore2` loggers to `INFO` when it
creates a client. Do not enable `DEBUG` for these loggers. This also overrides
`ANTHROPIC_LOG=debug`, which can expose request bodies in SDK debug logs.
