"""Possession: advisory locks with a vencimento.

A lock is the verb *I am*. It is deliberately not a fence (that is *you may not*), and it is
deliberately not durable: an unreleased lock dies on its own, because **a dead lock is a wall
in disguise**. Contention is enforced at the storage layer by a partial unique index over live
rows only, so correctness does not depend on every agent honoring the convention.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import store

_LOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS locks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resource    TEXT    NOT NULL,
    holder      TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    acquired_at TEXT    NOT NULL,
    expires_at  TEXT,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_lock ON locks(resource) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_locks_resource ON locks(resource, released_at);
"""


def _conn(db: Path) -> sqlite3.Connection:
    conn = store._conn(db)
    conn.executescript(_LOCK_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _live(conn: sqlite3.Connection, resource: str) -> Optional[sqlite3.Row]:
    """The current holder of *resource*, treating a lapsed vencimento as a dead lock.

    The row is stamped released here rather than in a sweeper: expiry is lazy by design, and
    this is the only place a lapsed lock is ever observed.
    """
    row = conn.execute(
        "SELECT * FROM locks WHERE resource = ? AND released_at IS NULL ORDER BY id DESC LIMIT 1",
        (resource,),
    ).fetchone()
    if row is None:
        return None
    if store._expired(row["expires_at"], _now()):
        conn.execute("UPDATE locks SET released_at = ? WHERE id = ?", (_now(), row["id"]))
        conn.commit()
        return None
    return row


def _denial(held: sqlite3.Row) -> dict[str, Any]:
    expiry = held["expires_at"]
    return {
        "granted": False,
        "held_by": held["holder"],
        "expires_at": expiry,
        "message": (
            f"Lock held by {held['holder']}"
            + (f" until {expiry}; wait for it to lapse or coordinate a release" if expiry
               else " with no vencimento — ask them to release it")
        ),
    }


def acquire(db: Path, *, resource: str, holder: str, expires_at: str | None = None,
            note: str = "") -> dict[str, Any]:
    """Take *resource* for *holder*, or explain who holds it and until when."""
    conn = _conn(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        held = _live(conn, resource)
        if held is not None and held["holder"] != holder:
            denial = _denial(held)
            conn.commit()
            return denial
        if held is not None:  # re-entrant: same holder re-asserts its own claim
            conn.execute(
                "UPDATE locks SET expires_at = COALESCE(?, expires_at) WHERE id = ?",
                (expires_at, held["id"]),
            )
            conn.commit()
            return {"granted": True, "lock_id": held["id"], "expires_at": expires_at,
                    "reentrant": True}
        cur = conn.execute(
            "INSERT INTO locks (resource, holder, note, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (resource, holder, note, _now(), expires_at),
        )
        conn.commit()
        return {"granted": True, "lock_id": int(cur.lastrowid), "expires_at": expires_at,
                "reentrant": False}
    except sqlite3.IntegrityError:
        # The partial unique index is the real guard against two writers racing past the
        # BEGIN IMMEDIATE above. Report it in the same shape as a logical denial.
        conn.rollback()
        held = _live(conn, resource)
        return _denial(held) if held is not None else {
            "granted": False, "held_by": "", "expires_at": None,
            "message": f"Lock on {resource} was taken concurrently",
        }
    except Exception:
        conn.rollback()
        raise


def release(db: Path, *, resource: str, holder: str, note: str = "") -> bool:
    conn = _conn(db)
    cur = conn.execute(
        "UPDATE locks SET released_at = ?, note = COALESCE(NULLIF(?, ''), note) "
        "WHERE resource = ? AND holder = ? AND released_at IS NULL",
        (_now(), note, resource, holder),
    )
    conn.commit()
    return cur.rowcount > 0


def renew(db: Path, lock_id: int, *, holder: str, expires_at: str | None) -> bool:
    conn = _conn(db)
    cur = conn.execute(
        "UPDATE locks SET expires_at = ? WHERE id = ? AND holder = ? AND released_at IS NULL",
        (expires_at, lock_id, holder),
    )
    conn.commit()
    return cur.rowcount > 0


def get_lock(db: Path, lock_id: int) -> Optional[dict[str, Any]]:
    row = _conn(db).execute("SELECT * FROM locks WHERE id = ?", (lock_id,)).fetchone()
    return store._row(row)


def live_locks(db: Path) -> list[dict[str, Any]]:
    """Currently-held locks, with lapsed ones reaped as a side effect of asking."""
    conn = _conn(db)
    now = _now()
    rows = conn.execute(
        "SELECT * FROM locks WHERE released_at IS NULL ORDER BY id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        if store._expired(r["expires_at"], now):
            conn.execute("UPDATE locks SET released_at = ? WHERE id = ?", (now, r["id"]))
            continue
        out.append(store._row(r) or {})
    conn.commit()
    return out
