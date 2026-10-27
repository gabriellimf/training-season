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
