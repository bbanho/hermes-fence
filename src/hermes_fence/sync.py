"""Pull: sync-on-intent via a per-reader cursor.

The load-bearing invariant: **an agent syncs the delta since its own last read, never the
current state of everyone else.** This is what makes "sync on nearly every multi-step
intention" affordable — an idle peer contributes zero entries, so steady-state token cost is
~zero. Without the cursor the design degenerates into an expensive distributed system prompt.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import store

_SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
    agent      TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    intent     TEXT    NOT NULL DEFAULT '',
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (agent, key)
);
CREATE TABLE IF NOT EXISTS sync_cursors (
    reader   TEXT    NOT NULL,
    agent    TEXT    NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT   NOT NULL,
    PRIMARY KEY (reader, agent)
);
CREATE INDEX IF NOT EXISTS idx_state_agent_seq ON agent_state(agent, seq);
"""


def _conn(db: Path) -> sqlite3.Connection:
    conn = store._conn(db)
    conn.executescript(_SYNC_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish(db: Path, *, agent: str, intent: str, payload: Optional[dict[str, Any]] = None,
            key: str = "", expires_at: str | None = None) -> int:
    """Publish (or replace) one agent's state under *key*, returning its new monotonic seq.

    State is a **current value per (agent, key)**, not a log: republishing the same key
    overwrites and bumps the seq, so a peer that syncs sees the current truth rather than
    every restatement the agent made on the way there.

    ``seq`` is monotonic **per agent**, not per key — the sync cursor is keyed
    ``(reader, agent)``, so a counter that restarted per key would make a later key's seq 1
    permanently invisible behind an earlier key's seq 3.
    """
    conn = _conn(db)
    high = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM agent_state WHERE agent = ?", (agent,)
    ).fetchone()["m"]
    seq = int(high) + 1
    conn.execute(
        "INSERT INTO agent_state (agent, key, seq, intent, payload, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(agent, key) DO UPDATE SET seq = excluded.seq, intent = excluded.intent, "
        "payload = excluded.payload, created_at = excluded.created_at, expires_at = excluded.expires_at",
        (agent, key or intent, seq, intent, json.dumps(payload or {}), _now(), expires_at),
    )
    conn.commit()
    return seq


def sync(db: Path, *, reader: str, agents: list[str], limit: int = 20) -> list[dict[str, Any]]:
    """Return the delta for *agents* since *reader*'s own cursor, then advance that cursor.

    Cursors are per ``(reader, agent)`` and only for the agents actually requested, so a
    reader can sync narrowly without losing its place on the peers it did not ask about.
    """
    if not agents:
        return []
    conn = _conn(db)
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        out: list[dict[str, Any]] = []
        for agent in agents:
            cur = conn.execute(
                "SELECT last_seq FROM sync_cursors WHERE reader = ? AND agent = ?",
                (reader, agent),
            ).fetchone()
            last = cur["last_seq"] if cur else 0
            rows = conn.execute(
                "SELECT * FROM agent_state WHERE agent = ? AND seq > ? "
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY seq ASC LIMIT ?",
                (agent, last, now, limit),
            ).fetchall()
            for r in rows:
                out.append({
                    "agent": r["agent"],
                    "key": r["key"],
                    "seq": r["seq"],
                    "intent": r["intent"],
                    "payload": store._decode_scope(r["payload"]),
                    "created_at": r["created_at"],
                })
            high = rows[-1]["seq"] if rows else last
            conn.execute(
                "INSERT INTO sync_cursors (reader, agent, last_seq, synced_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(reader, agent) DO UPDATE SET last_seq = excluded.last_seq, "
                "synced_at = excluded.synced_at",
                (reader, agent, high, now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


def cursor_position(db: Path, *, reader: str, agent: str) -> int:
    row = _conn(db).execute(
        "SELECT last_seq FROM sync_cursors WHERE reader = ? AND agent = ?", (reader, agent)
    ).fetchone()
    return int(row["last_seq"]) if row else 0


def known_agents(db: Path) -> list[str]:
    """Every agent that has ever published state — the address book for a ``sync all``."""
    return [r["agent"] for r in _conn(db).execute(
        "SELECT DISTINCT agent FROM agent_state ORDER BY agent"
    ).fetchall()]
