import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path("reminders.db")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER,
                channel_id  INTEGER NOT NULL,
                message     TEXT    NOT NULL,
                due_at      TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                snooze_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_due ON reminders (due_at);
            CREATE INDEX IF NOT EXISTS idx_user ON reminders (user_id);

            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id         INTEGER PRIMARY KEY,
                fallback_channel INTEGER
            );
        """)


# ── reminders ────────────────────────────────────────────────────────────────

def add_reminder(
    *,
    user_id: int,
    channel_id: int,
    message: str,
    due_at: datetime,
    guild_id: int | None = None,
) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO reminders (user_id, guild_id, channel_id, message, due_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, guild_id, channel_id, message, due_at.isoformat()),
        )
        return cur.lastrowid


def get_due_reminders(now: datetime) -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM reminders WHERE due_at <= ?",
            (now.isoformat(),),
        ).fetchall()


def get_user_reminders(user_id: int) -> list[sqlite3.Row]:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM reminders WHERE user_id = ? ORDER BY due_at",
            (user_id,),
        ).fetchall()


def get_reminder(reminder_id: int, user_id: int) -> sqlite3.Row | None:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        ).fetchone()


def delete_reminder(reminder_id: int, user_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )
        return cur.rowcount > 0


def snooze_reminder(reminder_id: int, user_id: int, new_due: datetime) -> bool:
    with _conn() as con:
        cur = con.execute(
            """UPDATE reminders
               SET due_at = ?, snooze_count = snooze_count + 1
               WHERE id = ? AND user_id = ?""",
            (new_due.isoformat(), reminder_id, user_id),
        )
        return cur.rowcount > 0


def clear_user_reminders(user_id: int) -> int:
    with _conn() as con:
        cur = con.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
        return cur.rowcount


# ── guild config ──────────────────────────────────────────────────────────────

def set_fallback_channel(guild_id: int, channel_id: int) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO guild_config (guild_id, fallback_channel)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET fallback_channel = excluded.fallback_channel""",
            (guild_id, channel_id),
        )


def get_fallback_channel(guild_id: int) -> int | None:
    with _conn() as con:
        row = con.execute(
            "SELECT fallback_channel FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row["fallback_channel"] if row else None

