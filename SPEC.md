# training-season

A self-hosted running coach. It reads your Garmin data, an LLM writes your training
week, and the structured workouts land on your watch.

One user per deployment. Self-hosted. MIT.

## Scope

Running only. The target device is a Garmin Forerunner 165 Music, which cannot
receive structured swim or strength workouts (see [Device constraints](#device-constraints)).

## The loop

1. **Onboarding**, in the chat. The coach asks what it needs, the user answers, and
   it builds a macrocycle to the target race.
2. **Every Sunday at 17:00 `America/Sao_Paulo`**, a cron job pulls Garmin data,
   generates the next week, validates its shape, and schedules the workouts on the
   Garmin Connect calendar. The watch picks them up on its next sync.
3. **Check-in**, whenever the user wants. Three fixed questions first: fatigue 1-10,
   whether anything hurts, and where, 1-10. Then free conversation.
4. **Silence is handled.** If the user never checks in, the week is still pushed. The
   coach is told there was no check-in, how many weeks in a row, and how many weeks
   remain until the race, so it can replan the time that is left.

Nothing blocks the push. The user never approves a plan.

## Garmin

### Reading

`python-garminconnect` (cyberjunky). Unofficial. Garmin's own Personal API tier,
free and self-service since 2026-10-21, is pull-only and cannot push workouts, so
there is no official path that closes this loop.

Pulled weekly:

- Activities: type, date, distance, duration, average and max heart rate, average
  pace, splits, cadence.
- Daily wellness: resting heart rate, sleep, Body Battery, stress, HRV.
- VO2max.

Full history on the first run. Deltas after that.

Swim activities are pulled and shown to the coach **read-only**. It plans nothing and
pushes nothing for them, but it must know they happened, or it will progress running
volume as if the athlete were rested.

Not pulled: Training Status, Training Load. The Forerunner 165 does not have them.
It is also not eligible to be a Primary Training Device.

### Writing

Running workouts are created in Garmin Connect and scheduled on the calendar, one
week at a time. Pace range is the primary target on every step.

### Device constraints

All confirmed from Garmin's own documentation and the Forerunner 165 owner's manual.

| Constraint | Value |
| --- | --- |
| Workout name | 15 characters. The device compares only the first 15. |
| Steps per workout | 50 |
| Repeats | 99 |
| Workouts stored on device | 25 |
| Calendar | A scheduled push **overwrites the entire training calendar** |
| Structured pool swim | **Not supported on this model** |
| Structured open water swim | Not supported |
| Structured strength | Supported, but out of scope |
| Running Power | Native, so power zone targets are available |

Two names that match in their first 15 characters are treated as the same workout,
and one silently overwrites the other.

**Naming rule.** `DD/MM` plus content, ASCII only, no accents:

```
26/10 10x400
30/10 Long 14k
02/11 Leve 6k
```

Code asserts the name is 15 characters or fewer and fails loudly. The full Portuguese
description, with accents, lives in the step notes and in the chat, where nothing is
truncated.

**Timezone.** `schedule_workout()` sends a bare `YYYY-MM-DD` with no timezone. Every
date is computed in `America/Sao_Paulo`, never UTC, or workouts land a day off.

**Before the first push**, the user turns off any Garmin Coach or Adaptive Training
Plan. Two coaches cannot write the same calendar.

## The coach

The coaching instruction is a **file in the repo**, not code. Anyone can replace it,
in any language, with no code change.

| Job | Model | Effort |
| --- | --- | --- |
| Sunday plan generation | Claude Opus | medium |
| Chat turns | Claude Sonnet | medium |

Both behind one adapter.

### Macro plan

Generated once at onboarding. Revised only explicitly, with a recorded reason. Old
versions are kept as revisions. The user can reopen onboarding at any time to change
the goal, which archives the current macro and rebuilds from that day forward.

### Memory

Cost must stay flat across a 30-week block.

- A running state-of-the-athlete summary that the coach maintains itself.
- The last four weeks in full detail.
- Older weeks as weekly totals only.
- The macro plan.

### Output validation

The model returns JSON validated against a Pydantic schema: valid step types, pace as
metres per second, at most 50 steps, name at most 15 characters. On failure, retry
once with the error attached. On a second failure, push nothing and report in the chat.

This checks the envelope only. It never second-guesses the training decisions. The
chat displays weekly kilometres against the previous week and the easy/hard split
next to each plan, so a bad week is visible without anything blocking it.

## Stack

| | |
| --- | --- |
| Language | Python |
| Web | FastAPI, serving the chat page as static files |
| Database | SQLite, on the droplet disk |
| Scheduler | cron |
| Host | DigitalOcean Droplet, $4/month |
| TLS | Caddy, Let's Encrypt, free subdomain |
| UI language | Portuguese |
| Docs language | English |

Python is not a preference. `python-garminconnect` is the only maintained library in
any language that can push structured workouts to Garmin. No JavaScript or TypeScript
equivalent exists.

## Secrets

Everything through environment variables. Nothing hardcoded, no dates and no personal
details anywhere in the code.

```
GARMIN_EMAIL
GARMIN_PASSWORD
ANTHROPIC_API_KEY
APP_PASSWORD
BREVO_API_KEY
BACKUP_EMAIL_FROM
BACKUP_EMAIL_TO
TZ=America/Sao_Paulo
DATABASE_PATH
GARMIN_TOKEN_DIR
```

The Garmin token is cached on disk and refreshes itself. If Garmin requires MFA, the
chat shows an input and `resume_login(client_state, mfa_code)` completes it, which
works across processes and servers.

The database, the token cache and `.env` are gitignored from the first commit.

## Auth

One password in an environment variable plus a signed session cookie. No user table,
no registration, no password reset. Rate limiting on the login.

## Backup

A weekly cron gzips the database and emails it to the owner.

DigitalOcean blocks outbound ports 25, 465 and 587 on every droplet, with no documented
self-service unblock, so plain SMTP cannot work from this host. The relay is **Brevo
over its HTTPS REST API**: free at 300 emails a day, no custom domain, and a static API
key instead of an OAuth token that can expire.

The job checks the file size first. Gmail accepts 25 MB and silently converts anything
larger into a Drive link rather than bouncing.

## Accepted risks

- **Pace ranges may collapse to a single average on the watch.** Confirmed on Fenix 8
  since September 2026, unverified on the Forerunner 165. The first real week is the
  test. If it collapses, quality sessions move to heart rate zone targets.
- **`python-garminconnect` is unofficial** and Garmin actively fights automated
  clients. Blocking is mostly TLS fingerprint based, which the library handles with
  `curl_cffi`. The maintainer patches it continuously.
- **Logging in from a cloud IP is sometimes blocked.** Fallback is importing a locally
  minted token, documented in the README as troubleshooting.
- **The Garmin refresh token lifetime is undocumented.** Expect occasional
  re-authentication.

## Out of scope

Swim and strength workouts on the watch. Multiple users. The official Garmin API.
FIT file sideload over USB. A mobile app.
