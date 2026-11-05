# LLM adapter

Every model request goes through `training_season.llm`. Callers import only that
module. It exposes the jobs (`LLMJob.PLAN`, `LLMJob.CHAT`), `ChatTurn`, `LLMReply`
(text, model, stop reason, request id, usage with token counts and cost), the
`LLMError` tree, `llm_config_from_environment()` and `new_llm_adapter()`.

## Provider selection

`LLM_PROVIDER` selects the provider:

| Value | Provider |
| --- | --- |
| empty or unset | `openai_compatible` (OpenRouter by default) |
| `openai_compatible` | `openai_compatible` |
| `anthropic` | Claude through `anthropic==2.3.0` |

A change of provider is an environment change only. The adapter reads and validates
only the settings of the selected provider. Settings of the other provider are
ignored, also when they are invalid.

Each consumer must call `llm_config_from_environment()` first. Do this before Garmin
configuration, database access, or any other work. Pass the result to
`new_llm_adapter()`. A missing or invalid setting raises `LLMConfigurationError`.

An unknown value fails with:

```
LLM_PROVIDER must be openai_compatible or anthropic. Got: 'openrouter'.
```

The message shows the value only when it has 64 characters or less and does not
contain an API key.

Run the local check before starting a consumer:

```sh
uv run training-season-llm check
docker compose run --rm app training-season-llm check
```

The check validates configuration. It does not create a client or contact a
provider. It exits with code `2` and a clear message when a setting is missing or
invalid. On success, it prints one line per job:

```
plan provider=openai_compatible model=nvidia/nemotron-3-ultra-550b-a55b:free
chat provider=openai_compatible model=nvidia/nemotron-3-ultra-550b-a55b:free
```

```
plan provider=anthropic model=claude-opus-5-5 effort=medium
chat provider=anthropic model=claude-sonnet-5-5 effort=medium
```

It prints `effort=none` when a Claude job has no configured effort.

## OpenAI-compatible provider

The adapter sends synchronous, non-streaming `POST {base_url}/chat/completions`
requests with `httpx2`.

| Variable | Default | Rule |
| --- | --- | --- |
| `OPENAI_COMPATIBLE_API_KEY` | none, required | Must not be blank. Printable ASCII only, no spaces |
| `OPENAI_COMPATIBLE_BASE_URL` | `https://openrouter.ai/api/v1` | `https` only, a host, no user, password, query or fragment, must not contain the key. A trailing `/` is removed |
| `OPENAI_COMPATIBLE_PLAN_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | ASCII, no whitespace, at most 128 characters, must not contain the key |
| `OPENAI_COMPATIBLE_CHAT_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | Same as the plan model |

A missing key fails with:

```
OPENAI_COMPATIBLE_API_KEY is not set and LLM_PROVIDER is openai_compatible (the default). Set the key, or set LLM_PROVIDER=anthropic, in the environment or the Compose env file before starting.
```

The key goes in the `Authorization: Bearer` header, never in the URL. The client
does not follow redirects, so the key never goes to a redirect target.

### Requests

The body has `model`, `max_tokens` and `messages`: the system text first, then the
turns in order. It has no `stream`, `tools` or `reasoning` keys. Plan requests allow
32,000 output tokens and use a 900-second timeout. Chat requests allow 16,000 output
tokens and use a 600-second timeout. Both use a 5-second connect timeout.

The reply text is `choices[0].message.content` only. Reasoning fields are ignored.
The request id is the response `id` when it is a short code such as `gen-…`.

### Retries

The adapter makes at most three attempts. It retries connection errors, timeouts,
HTTP 408, 409, 429 and 5xx, and an HTTP 200 with an embedded error (`finish_reason`
`error`, or a top-level `error` object) that holds such a code. These retries are within one adapter call.

- A `Retry-After` header with a value above 0 sets the wait.
- A `Retry-After` above 60 seconds stops the retries at once. Free-tier daily limits
  send long values, and a run must not hang.
- With no usable header, the wait is `min(0.5 × 2ⁿ, 8)` seconds with up to 25%
  jitter, the same as the Anthropic SDK.

### Errors

| Response | Error | Retried |
| --- | --- | --- |
| 401 | `LLMAuthenticationError` | no |
| 429 | `LLMRateLimitedError` | yes |
| 408, 409, 5xx, connection error, timeout | `LLMUnavailableError` | yes |
| 400, 402, 403, 404, 413, 422 and other 4xx | `LLMRequestRejectedError` | no |
| 200 with `finish_reason: "error"` | as the status in `choices[0].error.code`, else 502 | as the status |
| 200 with a top-level `error` object | as the status in `error.code`, else 502, like the row above | as the status |
| 200 that is not valid JSON or has a bad shape | `LLMResponseError` | no |
| 200 with `finish_reason: "length"` | `LLMResponseError` (max_tokens limit) | no |
| 200 with `finish_reason: "content_filter"` | `LLMRefusalError` | no |
| 200 with `stop` and empty content, or another finish reason | `LLMResponseError` | no |

OpenRouter uses 402 for insufficient credits and 403 for guardrail or moderation
blocks, so both map to rejected. Partial content in an error response is discarded.
Error messages hold only adapter text, the HTTP status and a safe error type, never
server text.

### Cost

The cost comes from `usage.cost` in the response. The free model reports `0`. The log
shows `unknown` when the response has no cost.

## Claude provider

Set `LLM_PROVIDER=anthropic`. The adapter uses `anthropic==2.3.0` with the
synchronous, non-streaming Messages API.

`ANTHROPIC_API_KEY` is required. A missing key fails with:

```
ANTHROPIC_API_KEY is not set. Set it in the environment or the Compose env file before starting.
```

The defaults are:

| Job | Model | Effort |
| --- | --- | --- |
| Sunday plan | `claude-opus-5-5` | `medium` |
| Chat | `claude-sonnet-5-5` | `medium` |

Set `CLAUDE_PLAN_MODEL` or `CLAUDE_CHAT_MODEL` to change one model. Each change uses
one environment variable. Set `CLAUDE_PLAN_EFFORT` or `CLAUDE_CHAT_EFFORT` to one of
`low`, `medium`, `high`, `xhigh`, `max`, or `none`. `none` omits `output_config`.
The adapter sends the selected effort explicitly.

The SDK also reads `ANTHROPIC_BASE_URL` and `ANTHROPIC_CUSTOM_HEADERS` from the
environment. An explicit `api_key` from `ANTHROPIC_API_KEY` takes precedence over
credential environment values.

### Requests and retries

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

HTTP 401 and 403 map to `LLMAuthenticationError`.

### Cost

The estimate uses the response model and usage counts. Prices are USD per million
tokens. The values come from
<https://platform.claude.com/docs/en/about-claude/pricing>, read 2026-10-27.

| Model ID | Input | 5-minute cache write | 1-hour cache write | Cache read | Output |
| --- | ---: | ---: | ---: | ---: | ---: |
| `claude-opus-5-5` | 4 | 5 | 8 | 0.20 | 20 |
| `claude-sonnet-5-5` | 2 | 2.50 | 4 | 0.10 | 10 |

The adapter logs `unknown` cost for other response models, refusals, non-standard
service tiers, fast inference, non-global inference geography, or cache writes
without a cache TTL breakdown.

## Logs

Each completed call logs one `INFO` line with the job, model, stop or finish reason,
token counts, cost, duration, and request id. The Claude line also has the configured
effort. A failed or malformed response logs one `WARNING` line with the job,
configured model, adapter error type, HTTP status, provider error type, request id,
and duration. Usage is not stored in SQLite.

The adapters never log prompts, turns, system text, responses, reasoning, thinking,
refusal explanations, server messages, or API keys.

When an adapter creates a client, it raises the `httpx2` and `httpcore2` loggers (and
`anthropic` for Claude) to `INFO` if their levels are below `INFO`. It preserves
`WARNING` and higher levels. This overrides `ANTHROPIC_LOG=debug`, which can expose
request bodies in SDK debug logs.

## Out of scope

Streaming, async calls, tools, structured output, prompt caching, reasoning control,
the OpenRouter retry of HTTP 402 for the in-flight budget, and plain-http base URLs.
