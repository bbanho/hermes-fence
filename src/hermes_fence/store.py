"""Shared operational store for mini hooks, the broadcast stack, and fences.

One SQLite file at the canonical Hermes root, shared by every profile and session.
Design rules that the tests pin down:

* **Expiry is lazy.** ``expires_at`` is filtered on read and swept by ``sweep_expired``;
  there is no daemon and no cron job. A dead hook costs nothing when nobody reads it.
* **Expiry retires, never deletes.** Expired rows stay for audit — a fence is a record of
  a decision, not a scratchpad entry.
* **Delivery is exactly-once per reader.** ``broadcast_reads`` is the dedup ledger, so a
  session that reads twice does not see a message twice, and one reader draining the stack
  never starves another.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT    NOT NULL,
    owner       TEXT    NOT NULL DEFAULT '',
    trigger     TEXT    NOT NULL DEFAULT 'always',
    scope       TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL,
    expires_at  TEXT,
    recalled_at TEXT,
    retired_at  TEXT
);

CREATE TABLE IF NOT EXISTS broadcasts (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    priority   TEXT NOT NULL DEFAULT 'normal',
    address    TEXT NOT NULL DEFAULT 'all',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS broadcast_reads (
    reader TEXT    NOT NULL,
    seq    INTEGER NOT NULL,
    read_at TEXT   NOT NULL,
    PRIMARY KEY (reader, seq)
);

CREATE TABLE IF NOT EXISTS fences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_pattern TEXT   NOT NULL,
    territory   TEXT    NOT NULL DEFAULT '',
    scope       TEXT    NOT NULL DEFAULT '{}',
    rule        TEXT    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    owner       TEXT    NOT NULL DEFAULT '',
    relax_note  TEXT,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT,
    relaxed_at  TEXT,
    retired_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_hooks_expiry ON hooks(expires_at);
CREATE INDEX IF NOT EXISTS idx_broadcasts_expiry ON broadcasts(expires_at);
CREATE INDEX IF NOT EXISTS idx_fences_expiry ON fences(expires_at);
CREATE INDEX IF NOT EXISTS idx_reads_reader ON broadcast_reads(reader, seq);
"""

_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(expires_at: Optional[str], now: str) -> bool:
    """ISO-8601 lexicographic comparison is correct for same-offset UTC stamps.

    Both sides are produced by :func:`_now` or validated on the way in, so a missing
    expiry means "never" and a malformed one is treated as expired rather than trusted.
    """
    if not expires_at:
        return False
    try:
        return expires_at <= now
    except TypeError:  # pragma: no cover - defensive
        return True


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema. Idempotent.

    ``CREATE TABLE IF NOT EXISTS`` never adds a column to a table that already exists, so a
    store created by an earlier version stays silently missing that column and every query
    fails with ``no such column``. Bumping ``user_version`` is the only thing that makes
    this run, and it is cheap.

    Adding a column is a metadata-only operation in SQLite, so it is safe against the other
    sessions holding the same file open under WAL.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= _SCHEMA_VERSION:
        return

    def columns(table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    if version < 1 and "territory" not in columns("fences"):
        conn.execute("ALTER TABLE fences ADD COLUMN territory TEXT NOT NULL DEFAULT ''")
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def _conn(path: Path) -> sqlite3.Connection:
    """One connection per thread; SQLite objects are not shareable across threads."""
    key = str(path)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    conn = cache.get(key)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
        cache[key] = conn
    _local.conns = cache
    return conn


def open_db(path: Path) -> Path:
    """Create or upgrade the schema if needed and return the db path (idempotent)."""
    path = Path(path)
    _conn(path)
    return path


def schema_version(db: Path) -> int:
    """Current schema version of *db* — the migration's own report card."""
    return int(_conn(db).execute("PRAGMA user_version").fetchone()[0])


def _row(r: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    return dict(r) if r is not None else None


def _row_scoped(r: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    """Row dict with the JSON ``scope`` column decoded."""
    d = _row(r)
    if d is not None:
        d["scope"] = _decode_scope(d.get("scope"))
    return d


def _decode_scope(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


# --- expiry ----------------------------------------------------------------

def sweep_expired(db: Path) -> dict[str, int]:
    """Stamp rows that have passed their vencimento. Rows are kept: expiry retires, never erases.

    The stamp is what makes ``list_fences``/``get_*`` able to distinguish "expired on its own"
    from "retired by the sweep" — the sweep is bookkeeping for humans, not the enforcement path.
    Enforcement is the read-side filter, so a row that nobody reads is never touched at all.
    """
    conn = _conn(db)
    now = _now()
    counts: dict[str, int] = {}
    for table in ("hooks", "broadcasts", "fences"):
        cur = conn.execute(
            f"UPDATE {table} SET retired_at = ? "
            f"WHERE retired_at IS NULL AND expires_at IS NOT NULL AND expires_at <= ?",
            (now, now),
        )
        counts[table] = cur.rowcount or 0
    conn.commit()
    return counts


# --- mini hooks ------------------------------------------------------------

def add_hook(
    db: Path, *, text: str, owner: str = "", trigger: str = "always",
    scope: Optional[dict[str, Any]] = None, expires_at: Optional[str] = None,
) -> int:
    conn = _conn(db)
    cur = conn.execute(
        "INSERT INTO hooks (text, owner, trigger, scope, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (text, owner, trigger, json.dumps(scope or {}), _now(), expires_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_hooks(
    db: Path, *, agent: str = "", now: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Live (unexpired, unrecalled) hooks visible to *agent*, oldest first."""
    now = now or _now()
    rows = _conn(db).execute(
        "SELECT * FROM hooks WHERE recalled_at IS NULL AND expires_at IS NULL "
        "UNION ALL "
        "SELECT * FROM hooks WHERE recalled_at IS NULL AND expires_at > ?",
        (now,),
    ).fetchall()
    return [h for h in (_row(r) for r in rows)
            if scope_contains(_decode_scope(h["scope"]), agent)]


def get_hook(db: Path, hook_id: int) -> Optional[dict[str, Any]]:
    return _row_scoped(_conn(db).execute("SELECT * FROM hooks WHERE id = ?", (hook_id,)).fetchone())


def recall_hook(db: Path, hook_id: int) -> bool:
    conn = _conn(db)
    cur = conn.execute("UPDATE hooks SET recalled_at = ? WHERE id = ?", (_now(), hook_id))
    conn.commit()
    return cur.rowcount > 0


# --- the broadcast stack ---------------------------------------------------

def push_broadcast(
    db: Path, *, text: str, author: str = "", priority: str = "normal",
    to: str = "all", expires_at: Optional[str] = None,
) -> int:
    """Push onto the shared stack, addressed via the mux grammar (``all``, ``agent:x``,
    ``!agent:y``). The address is validated here, so an unparseable recipient can never
    reach the table and be delivered as a silent ``all``."""
    from .mux import parse_recipients
    parse_recipients(to)
    conn = _conn(db)
    cur = conn.execute(
        "INSERT INTO broadcasts (text, author, priority, address, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (text, author, priority, to, _now(), expires_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def pending_broadcasts(
    db: Path, *, reader: str, limit: int = 20, sticky_window: int = 0,
    now: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Unread, unexpired broadcasts addressed to *reader*, oldest first, marked read.

    Read-marking is a single transaction with the SELECT, so two turns racing in the same
    process cannot both claim the same message.

    ``sticky_window`` bounds **durable, unbounded-address** messages to the newest N per
    reader. An unexpiring ``all`` message otherwise accumulates forever and becomes an
    unbounded context injection; older rows stay in the table for audit, they just fall out
    of the working set. Bounded-address durable messages are never capped — a message aimed
    at named agents is not leaking.
    """
    now = now or _now()
    conn = _conn(db)
    from .mux import parse_recipients
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM broadcasts WHERE (expires_at IS NULL OR expires_at > ?) "
            "AND seq NOT IN (SELECT seq FROM broadcast_reads WHERE reader = ?) "
            "ORDER BY seq ASC",
            (now, reader),
        ).fetchall()
        candidates = [d for d in (_row(r) for r in rows) if d is not None]

        def _address(d: dict[str, Any]):
            return parse_recipients(d["address"])

        # Durable + unbounded-address messages are split out: only the NEWEST `sticky_window`
        # ever enter a turn. Taking the oldest instead would be a drain, not a window — it
        # would ship superseded context and starve the current one. The skipped rows are not
        # marked read, so they remain in the table for audit and simply never reach a context.
        durable_unbounded: list[dict[str, Any]] = []
        ordinary: list[dict[str, Any]] = []
        for d in candidates:
            if not _address(d).matches(reader):
                continue
            (ordinary if d["expires_at"] or _address(d).is_bounded else durable_unbounded).append(d)
        if sticky_window > 0 and len(durable_unbounded) > sticky_window:
            durable_unbounded = durable_unbounded[-sticky_window:]

        selected = sorted(ordinary + durable_unbounded, key=lambda d: d["seq"])[:limit]
        for d in selected:
            conn.execute(
                "INSERT OR IGNORE INTO broadcast_reads (reader, seq, read_at) VALUES (?, ?, ?)",
                (reader, d["seq"], now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return selected


def broadcast_history(db: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    return [_row(r) for r in _conn(db).execute(
        "SELECT * FROM broadcasts ORDER BY seq DESC LIMIT ?", (limit,)
    ).fetchall()]


# --- fences: bounds, not walls --------------------------------------------

def add_fence(
    db: Path, *, tool_pattern: str, rule: str, scope: Optional[dict[str, Any]] = None,
    reason: str = "", owner: str = "", expires_at: Optional[str] = None,
    territory: str = "",
) -> int:
    """A fence binds a tool inside a territory and/or a scope.

    It must carry a ``rule`` and, if it is scoped at all, a ``reason`` — an unexplained bound
    is exactly the opaque wall we refuse to build. ``territory`` is a regex over the canonical
    workdir; a bare ``*`` is refused, because a fence that binds the whole host is a wall
    wearing a fence's clothes.
    """
    if not rule.strip():
        raise ValueError("a fence needs a rule: a bound with nothing to satisfy is a wall")
    if (scope or territory) and not reason.strip():
        raise ValueError("a scoped fence needs a reason: unnamed bounds are opaque")
    from .territory import pattern_is_too_broad
    if territory and pattern_is_too_broad(territory):
        raise ValueError(
            f"territory pattern {territory!r} would bind every path; "
            "name a real tree, e.g. /repos/NEXUS/.*"
        )
    conn = _conn(db)
    cur = conn.execute(
        "INSERT INTO fences (tool_pattern, territory, scope, rule, reason, owner, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tool_pattern, territory, json.dumps(scope or {}), rule, reason, owner, _now(), expires_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def scope_contains(scope: dict[str, Any], agent: str) -> bool:
    """A scope is a whitelist of dimensions; an unset dimension matches every agent."""
    if not scope:
        return True
    for key, want in scope.items():
        if want in (None, "", "*"):
            continue
        if key == "agent":
            if agent != want:
                return False
        elif str(scope.get(key)) and str(agent) != str(want):  # pragma: no cover
            return False
    return True


def fences_for_tool(db: Path, *, tool_name: str) -> list[dict[str, Any]]:
    """Live fences bound to *tool_name* regardless of agent or territory.

    Scope filtering is the caller's job (it knows the agent and the workdir); this exists so a
    boundary scoped to agent A can still be *shown* to agent B. An invisible boundary is
    indistinguishable from an arbitrary one.
    """
    now = _now()
    rows = _conn(db).execute(
        "SELECT * FROM fences WHERE relaxed_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
        (now,),
    ).fetchall()
    hits = [d for d in (_row_scoped(r) for r in rows) if d and _pattern_matches(d["tool_pattern"], tool_name)]
    return sorted(hits, key=lambda d: -(len(d["scope"]) + bool(d.get("territory"))))


def all_live_fences(db: Path) -> list[dict[str, Any]]:
    """Every fence that has not been relaxed or expired, across all agents and territories.

    This is the *visibility* query — what a peer should be told exists. Never a binding
    query; binding goes through :func:`fences_for`, which knows the caller's territory.
    """
    now = _now()
    rows = _conn(db).execute(
        "SELECT * FROM fences WHERE relaxed_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
        (now,),
    ).fetchall()
    return [d for d in (_row_scoped(r) for r in rows) if d]


def fences_for(db: Path, *, tool_name: str, agent: str, territory: str = "") -> list[dict[str, Any]]:
    """Live fences that bind *agent* in *territory*, most specific first.

    A fence binds when its tool matches AND both its territory and its agent scope allow this
    caller. An empty dimension on the fence means "any", so an unscoped fence is a global
    bound and a territory-only fence binds every agent inside that tree — which is the case
    that matters when several differently-named agents share one workdir.

    A nested worktree is *path-wise* inside its parent checkout but is a separate checkout
    with its own index and branch, so a parent fence is reported as advisory, not applied.
    That keeps a boundary exactly as wide as the operator declared it.
    """
    from .territory import _crosses_a_checkout, matches as _territory_matches
    hits = []
    for d in fences_for_tool(db, tool_name=tool_name):
        pattern = d.get("territory") or ""
        if pattern:
            base = pattern.removesuffix(".*").rstrip("/")
            if not _territory_matches(pattern, territory):
                continue
            if _crosses_a_checkout(base, territory):
                continue  # separate checkout: visible, not binding
        if not scope_contains(d["scope"], agent):
            continue
        hits.append(d)
    return hits


def _pattern_matches(pattern: str, tool_name: str) -> bool:
    try:
        return re.fullmatch(pattern, tool_name) is not None
    except re.error:
        # A malformed pattern must not silently widen into "matches everything".
        return pattern == tool_name


def get_fence(db: Path, fence_id: int) -> Optional[dict[str, Any]]:
    return _row_scoped(_conn(db).execute("SELECT * FROM fences WHERE id = ?", (fence_id,)).fetchone())


def relax_fence(db: Path, fence_id: int, *, note: str = "") -> bool:
    """Release a fence's *binding* while keeping the row: the negotiation stays on record."""
    conn = _conn(db)
    cur = conn.execute(
        "UPDATE fences SET relaxed_at = ?, relax_note = ? WHERE id = ?",
        (_now(), note, fence_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_fences(db: Path, *, include_relaxed: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM fences"
    if not include_relaxed:
        sql += " WHERE relaxed_at IS NULL"
    sql += " ORDER BY id"
    return [_row_scoped(r) for r in _conn(db).execute(sql).fetchall()]
