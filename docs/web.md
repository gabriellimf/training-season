# Web chat page

The web server serves one page at `/` and a JSON API under `/api`. The page is the
chat with the coach on a phone or a desktop browser: onboarding, the plan, the
check-in and the Garmin code. The text is Portuguese. The page has two tabs,
`Conversa` and `Plano`.

The page has no login yet. Keep it on loopback until the password login exists.
See [Safety](#safety).

## Run

Use one of the two ways. Both use port 8000 on the host, so run only one of them.

With Compose:

```sh
docker compose up -d web
```

Without Docker:

```sh
uv run training-season-web
```

Open `http://127.0.0.1:8000`. The command takes no arguments. `--help` prints the
usage line.

To reload the code while you edit it, run uvicorn with the app factory:

```sh
uv run uvicorn --factory training_season.web_app:create_default_app --host 127.0.0.1 --port 8000
```

Run one process only. The coach lock and the check-in conversation live in the
memory of the process. `training-season-web` starts uvicorn with `workers=1`. A
hand-run `uvicorn --workers N` gives each worker its own lock and its own
conversation, so it breaks both. Use one worker.

Before it listens, the server checks the same settings as the other commands:
`TZ`, `DATABASE_PATH`, `GARMIN_TOKEN_DIR`, the Garmin credentials, the coach
instruction and the LLM provider. It migrates the database and builds the model
adapter once. A bad setting stops the command with exit code `2` and the error
text. A SQLite or migration failure stops it with exit code `1`.

## Environment

The page uses the settings of [onboarding.md](onboarding.md) and
[checkin.md](checkin.md). It adds these:

| Variable | Default | Use |
| --- | --- | --- |
| `WEB_HOST` | `127.0.0.1` | Address to listen on. A blank value is the default |
| `WEB_PORT` | `8000` | TCP port, an integer from 1 to 65535. The error names the value |
| `WEB_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host header values the server answers. Comma separated, no wildcard. A bad value stops the server |
| `WEB_PUBLISH_PORT` | `8000` | Compose only. The port on `127.0.0.1` of the host |

Compose sets `WEB_HOST` to `0.0.0.0` for the `web` service, because Docker port
publishing needs it inside the container: a server on the container loopback is not
reachable from the host. The container listens on port `8000`, and Compose
publishes it on `127.0.0.1` only, so the address is still private to the Mac:

```yaml
ports:
  - "127.0.0.1:${WEB_PUBLISH_PORT:-8000}:8000"
```

At start, the server logs one warning when `WEB_HOST` is not `127.0.0.1`, `::1` or
`localhost`. In the `web` container that warning is expected.

Compose does not pass `WEB_PORT`. Change `WEB_PUBLISH_PORT` to use another port on
the host. The `web` service shares the `data` volume with `app` and `scheduler`.

## Safety

The page has no login until the password login lands. These rules protect it on
loopback:

- Compose publishes on `127.0.0.1` only. Outside Docker, `WEB_HOST` is `127.0.0.1`
  by default.
- The Host allow-list (`WEB_ALLOWED_HOSTS`) refuses a request with any other Host
  header with 400. This blocks DNS rebinding. The port is not part of the check.
- Every method other than GET and HEAD must pass one guard, before any route runs
  and before the body is read. A browser sends a cross-site form or `text/plain`
  POST with no preflight, and a route with no body would run for it. The guard
  refuses, with the body `{status: "invalid", message: "Mensagem inválida."}`:
  - 403 when `Sec-Fetch-Site` is present and not `same-origin` or `none`, or when
    `Origin` is present and its host is not in `WEB_ALLOWED_HOSTS`.
  - 415 when the media type of `Content-Type` is not `application/json`, also
    when the header is missing. So the routes with no body need the body `{}`
    with `Content-Type: application/json`.
  - 411 when `Content-Length` is missing, for example a chunked body.
  - 413 when `Content-Length` is above 128 KiB. The largest valid body, a
    16 000-character text, is below 100 KB.
- A cross-site request with `Content-Type: application/json` needs a CORS
  preflight. The server sets no CORS headers, so the browser never sends it, and
  another site cannot read an answer.
- `POST /api/week-one` runs only in the mode `week_one_pending`. Else it gives 409,
  so a second tab or a double tap cannot plan and push week 1 again.
- The server sends no `server` header.
- Every response carries `Content-Security-Policy: default-src 'self';
  frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`. The page has no inline script or style, no
  external font and no CDN.
- Every `/api` response also carries `Cache-Control: no-store`, so the browser
  keeps no coach or athlete text in its disk cache. The page files keep their
  ETag caching.
- The page shows every model and athlete text as plain text. It parses no
  Markdown, so the `**bold**` marks of the coach show as asterisks.
- The page stores no text in the browser storage.
- Field caps: a chat `text` is at most 16 000 characters. A check-in sends at most
  4 answers of at most 400 characters. An MFA `code` is 1 to 32 characters.

Do not add a host to `WEB_ALLOWED_HOSTS` or publish the port on a network address
before the password login exists. Anyone who reaches the page can talk to the coach
(model cost), read the plan and the history (health data), and start a Garmin login
or send an MFA code.

## What the page shows

`GET /api/state` tells the page what to show. It reads SQLite and local files. It
makes no model call and no Garmin call. The first rule that fits gives the `mode`:

| `mode` | When | The page |
| --- | --- | --- |
| `onboarding` | An onboarding session is open. `reopening` is true when an active plan exists | The session messages and the input |
| `needs_onboarding` | No active macro plan | The intro and `Começar` |
| `week_one_pending` | An active macro plan and no stored week | The last message and `Enviar semana 1` |
| `ready` | An active macro plan and a stored week | The plan, the history and the actions `Fazer check-in` and `Revisar objetivo` |

A partial week-1 push stores the week, so the mode is `ready` and the days that
did not land show as not scheduled. Use `training-season-onboarding week` to send
week 1 again. See [onboarding.md](onboarding.md).

The state also holds:

- `checkin_open`: a check-in conversation is open in this process.
- `busy`: `null`, or `{job, started_at}` of the coach job that runs now. `job` is
  one of `onboarding_session`, `onboarding_turn`, `onboarding_discard`, `week_one`,
  `checkin_save`, `checkin_reply`, `checkin_end`, `garmin_login`, `garmin_mfa`.
- `garmin`: `{state, mfa_requested_at}`. `state` is `no_token`, `token_cached` or
  `mfa_pending`. The MFA panel shows when it is `mfa_pending`, in any mode. See
  [garmin-auth.md](garmin-auth.md#code-in-the-browser).
- `today`: the local training date.

## API

Every body is JSON and every text is Portuguese. Dates are `YYYY-MM-DD`. Times are
UTC, `...Z`.

| Method | Path | Body | Answer |
| --- | --- | --- | --- |
| GET | `/api/state` | none | The state above |
| GET | `/api/messages?limit=50` | none | The last user and coach messages, oldest first. `limit` is 1 to 200 |
| GET | `/api/onboarding` | none | `{session_id, reopening, intro, messages}` |
| POST | `/api/onboarding/session` | `{reopening}` | The same as GET. Opens or resumes the session |
| POST | `/api/onboarding/messages` | `{text}` | `{status, message, macro_plan_id}` |
| POST | `/api/onboarding/discard` | `{}` | `{status: "discarded", message}` |
| POST | `/api/week-one` | `{}` | `{status, message}`. 409 unless the mode is `week_one_pending` |
| GET | `/api/plan` | none | `{today, weeks}`: the stored weeks of the active plan that are not over |
| POST | `/api/checkin/answers` | `{answers}` | `{status, question, hint, accepted, answer_kind, message}` |
| GET | `/api/checkin` | none | `{open, intro, saved_message, messages}` |
| POST | `/api/checkin/messages` | `{text}` | `{status, message}` |
| POST | `/api/checkin/end` | `{}` | `{status: "ended", message}` |
| POST | `/api/garmin/login` | `{}` | `{status, message, garmin}` |
| POST | `/api/garmin/mfa` | `{code}` | `{status, message, garmin}` |

The `status` of a 200 answer:

| Route | Values |
| --- | --- |
| `/api/onboarding/messages` | `asking`, `completed`, `result_rejected`, `macro_rejected`, `message_too_long`, `session_too_long`, `model_failed` |
| `/api/week-one` | `pushed`, `partial`, `empty`, `rejected`, `mfa_required`, `failed` |
| `/api/checkin/answers` | `question`, `hint`, `saved` |
| `/api/checkin/messages` | `replied`, `message_too_long`, `conversation_too_long`, `model_failed`, `not_open` |
| `/api/garmin/login` | `connected`, `mfa_required`, `failed` |
| `/api/garmin/mfa` | `connected`, `code_rejected`, `not_pending`, `state_invalid`, `unavailable`, `failed` |

A chat outcome is always HTTP 200. The page acts on `status` and shows `message`.
Every other answer is an error with the body `{status, message}`:

| HTTP | `status` | Meaning |
| --- | --- | --- |
| 400 | none | The Host header is not in `WEB_ALLOWED_HOSTS` |
| 403, 411, 413, 415 | `invalid` | The request guard refused a write (see [Safety](#safety)). `Mensagem inválida.` |
| 404, 405 | `invalid` | An unknown `/api` path or a wrong method. `Mensagem inválida.` A missing page file keeps the plain 404 |
| 409 | `busy` | Another coach job runs. `O treinador ainda está trabalhando na última mensagem. Aguarde.` |
| 409 | `conflict` | The request does not fit the stored state, for example `Nenhuma sessão de onboarding aberta.` or `A semana 1 já foi enviada.` |
| 422 | `invalid` | The body is not valid. `Mensagem inválida.` The answer never repeats the sent value |
| 503 | `error` | SQLite failed. `O banco de dados falhou na leitura ou na gravação. Tente de novo.` |
| 500 | `error` | Any other error. `Algo deu errado no servidor. Tente de novo.` |

`GET /api/onboarding` and `GET /api/checkin` always return `intro`. The web intros
do not end with the `/sair` hint, because the page has no such command.

## Long calls

A model call or a Garmin call can take minutes. See [llm.md](llm.md).

- The server sets no request timeout.
- One coach job runs at a time. The jobs that take the lock are the onboarding
  session, message and discard, week 1, the check-in save, message and end, and the
  Garmin login and code. A second job gets 409 `busy` at once and waits for
  nothing. The read routes never take the lock.
- A dropped request does not stop the work. The job runs to the end and stores its
  result in SQLite, or in the check-in conversation.
- The page shows the waiting text with the elapsed time. After a network error, or
  when the tab shows again, it reads `/api/state` every 3 seconds while `busy` is
  set or the server is offline. Then it reloads the history and the plan.
- `POST /api/week-one` stores its outcome as a `message` row, so a dropped request
  still leaves the text in the history.

## Check-in conversation

The coach replies of a check-in are not stored. The open conversation lives in the
memory of the server process, one at a time. A page reload gets it back with
`GET /api/checkin`. A restart of the server ends it. Then `/api/checkin/messages`
answers `not_open`. The answers and your messages stay stored in the `checkin` row.
See [checkin.md](checkin.md).

## Privacy

Log lines, responses and errors never hold the athlete texts, the coach replies,
the MFA code, the token, the MFA state or an API key. A failed request logs the
method, the path and the error class only. The 500 and 503 answers use a fixed
Portuguese text, never the text of the error.
