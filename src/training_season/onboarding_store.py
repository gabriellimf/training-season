"""Onboarding sessions and their chat turns in the `message` table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from training_season.database import transaction
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    import sqlite3

SessionRole = Literal["user", "assistant"]

_INSERT_MESSAGE = """
INSERT INTO message (role, content, onboarding_session_id, created_at)
VALUES (?, ?, ?, ?)
"""
_COMPLETE_SESSION = """
UPDATE onboarding_session
SET status = 'completed', completed_at = ?, macro_plan_id = ?
WHERE id = ? AND status = 'open'
"""
_DISCARD_OPEN_SESSION = """
UPDATE onboarding_session SET status = 'discarded', discarded_at = ?
WHERE status = 'open'
RETURNING id
"""


class OnboardingSessionError(Exception):
    """The onboarding session is not in the state the write needs."""


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """One stored chat turn of an onboarding session."""

    role: SessionRole
    content: str


def current_open_session(connection: sqlite3.Connection) -> int | None:
    """
    Return the id of the open onboarding session, or None.

    Used as `session_id = current_open_session(connection)`
    """
    row = connection.execute(
        "SELECT id FROM onboarding_session WHERE status = 'open'"
    ).fetchone()
    return None if row is None else int(row[0])


def open_session(connection: sqlite3.Connection) -> int:
    """
    Start a new onboarding session. Fails while another one is open.

    Used as `session_id = open_session(connection)`
    """
    with transaction(connection):
        current = current_open_session(connection)
        if current is not None:
            raise OnboardingSessionError(
                f"onboarding session {current} is already open, "
                "expected no open session"
            )
        row = connection.execute(
            "INSERT INTO onboarding_session (status, started_at) "
            "VALUES ('open', ?) RETURNING id",
            (utc_now(),),
        ).fetchone()
    return int(row[0])


def _require_open(connection: sqlite3.Connection, session_id: int) -> None:
    row = connection.execute(
        "SELECT status FROM onboarding_session WHERE id = ?", (session_id,)
    ).fetchone()
    status = "missing" if row is None else str(row[0])
    if status != "open":
        raise OnboardingSessionError(
            f"onboarding session {session_id} is {status}, expected an open session"
        )


@dataclass(frozen=True, slots=True)
class SessionExchange:
    """One athlete message and the coach reply stored for it."""

    user_text: str = field(repr=False)
    assistant_text: str = field(repr=False)


def insert_exchange(
    connection: sqlite3.Connection, session_id: int, exchange: SessionExchange
) -> None:
    """
    Store one exchange of an open session, inside the caller's transaction.

    Used as `insert_exchange(connection, session_id, SessionExchange("Oi", "Olá"))`
    """
    _require_open(connection, session_id)
    now = utc_now()
    connection.executemany(
        _INSERT_MESSAGE,
        [
            ("user", exchange.user_text, session_id, now),
            ("assistant", exchange.assistant_text, session_id, now),
        ],
    )


def append_exchange(
    connection: sqlite3.Connection,
    session_id: int,
    user_text: str,
    assistant_text: str,
) -> None:
    """
    Store one user turn and its reply together, so history always alternates.

    Used as `append_exchange(connection, session_id, "Maratona", "Quando?")`
    """
    with transaction(connection):
        insert_exchange(
            connection, session_id, SessionExchange(user_text, assistant_text)
        )


def session_messages(
    connection: sqlite3.Connection, session_id: int
) -> tuple[SessionMessage, ...]:
    """
    Return the session's stored turns in the order they were written.

    Used as `history = session_messages(connection, session_id)`
    """
    rows = connection.execute(
        "SELECT role, content FROM message WHERE onboarding_session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return tuple(SessionMessage(row[0], row[1]) for row in rows)


def discard_open_session(connection: sqlite3.Connection) -> int | None:
    """
    Close the open session with no macro plan. Its messages stay stored.

    Returns the discarded session id, or None when no session is open.
    Used as `discarded_id = discard_open_session(connection)`
    """
    with transaction(connection):
        row = connection.execute(_DISCARD_OPEN_SESSION, (utc_now(),)).fetchone()
    return None if row is None else int(row[0])


def mark_session_completed(
    connection: sqlite3.Connection,
    session_id: int,
    macro_plan_id: int,
    completed_at: str,
) -> None:
    """
    Close the open session on its macro plan, inside the caller's transaction.

    Used as `mark_session_completed(connection, session_id, macro_plan_id, now)`
    """
    cursor = connection.execute(
        _COMPLETE_SESSION, (completed_at, macro_plan_id, session_id)
    )
    if cursor.rowcount != 1:
        raise OnboardingSessionError(
            f"onboarding session {session_id} is not open, "
            "expected one open session to complete"
        )
