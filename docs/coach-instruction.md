# Coaching instruction

The coach instruction is a file, not code. Replace the file to change the coach
language, tone, and training approach. The bundled example is
`src/training_season/instructions/coach.pt-BR.md`.

The rendered file is passed as the complete `system=` value. Code does not prepend
or append text to it. Code-owned output contracts, workout names, and device limits
belong in a user turn.

## Path

`COACH_INSTRUCTION_PATH` is optional. Leave it empty to use the bundled Portuguese
example. A configured path must be absolute. Set the path in the process environment
or in the Compose environment file. Do not store private profile data in the
instruction file.

The file must be UTF-8. A UTF-8 byte order mark is allowed. The file must contain
non-whitespace text and be no larger than 65,536 bytes.

## Template syntax

Use `$name` or `${name}` for a variable. Use `$$` for a literal dollar sign. Unknown
or malformed placeholders stop instruction loading. A literal dollar sign must be
escaped.

| Variable | Value |
| --- | --- |
| `today` | Current date in the training timezone, formatted as `YYYY-MM-DD`. |
| `athlete_name` | Name or nickname supplied by the profile. A missing or blank value is `-`. |
| `race_date` | Race date from the active macro plan, formatted as `YYYY-MM-DD`. A missing value is `-`. |
| `weeks_to_race` | Full weeks from `today` to `race_date`. Race week and past dates are `0`. A missing race date is `-`. |

The caller supplies profile and state values. The `-` marker means that a value is
not available yet.

## Language and code-owned rules

The coach follows the language requested by the instruction file. The bundled example
asks for Brazilian Portuguese. A replacement instruction can ask for German or
another language. Code does not force Portuguese on coach replies.

Do not put the output schema, workout name rules, or device limits in this file.
Those rules belong to code-owned user turns and validation.

## Loading and validation

Each process loads and validates the file at startup. A cron process reads the file
on its next run. A long-running process must restart after the file changes.

Run the local check:

```sh
uv run training-season-instruction check
```

Run the check in Docker:

```sh
docker compose run --rm app training-season-instruction check
```

The command exits with `0` when the file is valid and `2` when loading fails. It
prints the source, path, byte count, and placeholder names. It never prints the file
text.

## Docker replacement

Create `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  app:
    environment:
      COACH_INSTRUCTION_PATH: /config/instruction/coach.md
    volumes:
      - type: bind
        source: /absolute/host/directory
        target: /config/instruction
        read_only: true
        bind:
          create_host_path: false
```

Replace `/absolute/host/directory` with an existing absolute directory outside the
repository. Put the instruction file in that directory. Mount a directory, not one
file, so editor saves that replace a file continue to reach the container. Compose
merges this override automatically.

The container process runs as UID `999`. Make the directory traversable and the file
readable by that user. The read-only mount prevents container writes.

The override file is gitignored. Keep it out of commits. Restart a long-running
container after replacing the file.

## Privacy

The rendered instruction can contain profile data. The loader and renderer do not
log instruction text or rendered text.
