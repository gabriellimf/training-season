# Garmin authentication

The app stores Garmin authentication files in `GARMIN_TOKEN_DIR`. Docker Compose
sets it to `/data/garmin`. Outside Docker, set it to an absolute path whose parent
directory exists. The app creates the directory with mode `0700`.

The directory contains:

- `garmin_tokens.json`, the Garmin token cache.
- `garmin_mfa_state.json`, pending MFA state from Garmin.
- `garmin.lock`, the cross-process lock.

The token cache and MFA state are credentials. The MFA state contains Garmin session
cookies. Keep both files private. The app creates files with mode `0600`. Do not
commit them or include them in the SQLite backup.

Set `GARMIN_EMAIL` and `GARMIN_PASSWORD` in the process environment or Compose env
file. They are needed only when a credential login is required. Do not set
`GARMINTOKENS`.

## Compose password values

Compose interpolates `$NAME` in an unquoted env-file value. In the unquoted test, the
unset variable suffix was removed and Compose printed a warning. A single-quoted
value kept the suffix. `docker compose config` represented its literal dollar sign
as `$$`. Put the full password value in single quotes in the Compose env file when it
contains `$`.

## Login and MFA

`python-garminconnect` is pinned to version `0.3.20`. In this version,
`login(return_on_mfa=True)` returns JSON-serializable MFA state. `resume_login()` can
use that state from a new process.

Run the CLI from the project environment:

```sh
uv run training-season-garmin status
uv run training-season-garmin login
```

If Garmin requests MFA, `login` exits with code `3` and saves the pending state.
Enter the code through stdin. It is not a command-line argument.

```sh
uv run training-season-garmin mfa
```

In Docker Compose, run each command in its own container:

```sh
docker compose run --rm app training-season-garmin status
docker compose run --rm app training-season-garmin login
docker compose run --rm app training-season-garmin mfa
```

The interactive `mfa` command needs a TTY, so do not pass `-T`. Pass `-T` when
piping input to `mfa`.

The MFA state has no automatic expiry. If Garmin rejects an old code, run
`training-season-garmin login` to request a new one. This replaces the old pending
state.

Use `training-season-garmin check` to test a session. It contacts Garmin.
`training-season-garmin status` reads local files and does not contact Garmin.

Do not enable DEBUG logging for `garminconnect` in production. Version `0.3.20` can
log part of a failed token-refresh response at DEBUG level.

## Code in the browser

The [chat page](web.md) can take the MFA code, so you do not need a TTY. The page
reads the Garmin state from the token directory with `GET /api/state`. It makes no
Garmin call on load. The MFA panel shows when `garmin.state` is `mfa_pending`, in any
page mode. It has a numeric input for the code, `Enviar`, and `Pedir novo código`.
The code is never logged and never sent back. The input is cleared after each send.

To finish a login that stopped at MFA:

1. Run `docker compose run --rm app training-season-garmin login`. It exits with
   code `3` and saves the pending state.
2. Open the page. The MFA panel shows. The pending state is in the token directory,
   so a restart of `web` between the two steps does not lose it.
3. Enter the code and send it. On success the page shows `Login no Garmin concluído.
   O token foi salvo.` and the panel goes away.
4. Check with `docker compose run --rm app training-season-garmin status`. It prints
   `token_cached`.

`Pedir novo código` calls `POST /api/garmin/login`. It replaces the old pending state
and asks Garmin for a new code. With `garmin.state` `no_token`, the page shows
`Conectar o Garmin`, which calls the same route.

Both routes hold the coach lock, so a second job gets 409 `busy` while one runs. The
Portuguese text of each outcome:

| Route | Outcome | `status` | Text |
| --- | --- | --- | --- |
| mfa | Code accepted | `connected` | `Login no Garmin concluído. O token foi salvo.` |
| mfa | Garmin rejected the code | `code_rejected` | `O Garmin recusou o código. Confira o código e envie de novo.` |
| mfa | No login waits for a code | `not_pending` | `Nenhum login do Garmin espera código agora. Peça um novo código.` |
| mfa | The pending state cannot continue | `state_invalid` | `O login pendente do Garmin não pode continuar. Peça um novo código.` |
| mfa | Garmin did not answer, and the token is saved | `connected` | `Login no Garmin concluído. O token foi salvo.` |
| mfa | Garmin did not answer, and the login still waits for the code | `unavailable` | `O Garmin não respondeu. Envie o código de novo em alguns minutos.` |
| mfa | Garmin did not answer, and no token is saved | `unavailable` | `O Garmin não respondeu e o login não terminou. Peça um novo código.` |
| mfa | Another Garmin error | `failed` | `Não consegui concluir o login no Garmin: <cause>` |
| login | Garmin asks for a code | `mfa_required` | `O Garmin enviou um código de verificação. Digite o código abaixo.` |
| login | Login done | `connected` | `Login no Garmin concluído. O token foi salvo.` |
| login | Another Garmin error | `failed` | `Não consegui entrar no Garmin: <cause>` |

`<cause>` is a fixed Portuguese text per error type, never the Garmin error text. A
held Garmin lock adds `Tente de novo em alguns minutos.` Every Garmin answer also
carries `garmin: {state, mfa_requested_at}`.

### Login note

When a week push signs in with the e-mail and password, its chat text starts with one
note line. The page history shows it, and so does the weekly message. The note says
why the token was not used:

| Reason | Note |
| --- | --- |
| The saved token was rejected | `O Garmin recusou o token salvo, e o app entrou de novo com o e-mail e a senha.` |
| No token was saved | `Não havia token do Garmin salvo, e o app entrou com o e-mail e a senha.` |
| The saved token could not be read | `O token salvo do Garmin não pôde ser lido, e o app entrou de novo com o e-mail e a senha.` |

A push that used the saved token has no note.

## Import a locally created token

Use this only when Garmin blocks a credential login from the server. Create the
token on a machine that can log in. Set `GARMIN_TOKEN_DIR`, `GARMIN_EMAIL`, and
`GARMIN_PASSWORD` in that machine's environment.

```sh
uv run training-season-garmin login
uv run training-season-garmin mfa
```

The MFA command is needed only if Garmin requests a code. Send the token file to the
server over SSH and pass it to the import command on stdin:

```sh
ssh <droplet> 'cd <app-directory> && docker compose run --rm -T app training-season-garmin import-token' < <token-directory>/garmin_tokens.json
```

Verify the imported token:

```sh
ssh <droplet> 'cd <app-directory> && docker compose run --rm app training-season-garmin check'
```

Delete the local token file and any temporary copies after import. A copied token
grants access to the Garmin account until it is revoked in Garmin account security.
The library's `logout()` does not revoke a token.
