@~/.agents/ship/CLAUDE.md
@~/.agents/ship/gabriellimf-shared/ticket-artifacts.md

Open `~/.agents/ship/CLAUDE.md` and `~/.agents/ship/gabriellimf-shared/ticket-artifacts.md` with your file tool and follow them. The first holds the ship house rules for every seat; its Part 2 (the ticket gate) wins over any other text here. The second holds the company's tracker fields, QA audience and evidence forms.

# training-season

Self-hosted single-user running coach. Python 3.12, `uv`, SQLite, Docker Compose.
`SPEC.md` at the repo root is the product spec.

- Gate: `uv run ruff check && uv run ruff format --check && uv run mypy src tests && uv run pytest`
- UI strings are Portuguese. Docs, code and commit messages are English (`SPEC.md`,
  Stack table)
- `anthropic` and `garminconnect` are pinned in `pyproject.toml`. Read the CHANGELOG
  before any upgrade, as the comments there say
- No test calls Anthropic or Garmin. `tests/conftest.py` blocks the real httpx2
  transport. Adapter tests use `httpx2.MockTransport`
- Local runs outside Docker need `TZ`, `DATABASE_PATH` and `GARMIN_TOKEN_DIR`. Use
  paths under the git-ignored `data/` folder


## Code style

- Functions: 4-20 lines. Split if longer.
- Files: under 500 lines. Split by responsibility.
- One thing per function, one responsibility per module (SRP).
- Names: specific and unique. Avoid `data`, `handler`, `Manager`.
  Prefer names that return <5 grep hits in the codebase.
- Types: explicit. No `any`, no `Dict`, no untyped functions.
- No code duplication. Extract shared logic into a function/module.
- Early returns over nested ifs. Max 2 levels of indentation.
- Exception messages must include the offending value and expected shape.

## Comments

- Keep your own comments. Don't strip them on refactor — they carry
  intent and provenance.
- Write WHY, not WHAT. Skip `// increment counter` above `i++`.
- Docstrings on public functions: intent + one usage example.
- Reference issue numbers / commit SHAs when a line exists because
  of a specific bug or upstream constraint.

## Tests

- Tests run with a single command: `<project-specific>`.
- Every new function gets a test. Bug fixes get a regression test.
- Mock external I/O (API, DB, filesystem) with named fake classes,
  not inline stubs.
- Tests must be F.I.R.S.T: fast, independent, repeatable,
  self-validating, timely.

## Dependencies

- Inject dependencies through constructor/parameter, not global/import.
- Wrap third-party libs behind a thin interface owned by this project.

## Structure

- Follow the framework's convention (Rails, Django, Next.js, etc.).
- Prefer small focused modules over god files.
- Predictable paths: controller/model/view, src/lib/test, etc.

## Formatting

- Use the language default formatter (`cargo fmt`, `gofmt`, `prettier`,
  `black`, `rubocop -A`). Don't discuss style beyond that.

## Logging

- Structured JSON when logging for debugging / observability.
- Plain text only for user-facing CLI output.