"""Schema migration — the defect that would have broken every live session.

`CREATE TABLE IF NOT EXISTS` never adds a column to a table that already exists. The live
store at ``~/.hermes/hermes-fence/fence.db`` was created *before* the ``territory`` column
existed, so without a migration every fence query raises ``no such column: territory`` — in
every session, in every profile, silently, because the plugin swallows hook exceptions.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_fence import store

# The pre-territory schema, exactly as the live db has it.
LEGACY = """
CREATE TABLE IF NOT EXISTS fences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_pattern TEXT   NOT NULL,
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
"""


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    db = tmp_path / "fence.db"
    conn = sqlite3.connect(db)
    conn.executescript(store._SCHEMA.replace(
        "    territory   TEXT    NOT NULL DEFAULT '',\n", ""
    ))
    conn.execute(
        "INSERT INTO fences (tool_pattern, scope, rule, reason, owner, created_at, expires_at) "
        "VALUES ('terminal', '{}', 'legacy rule', 'from before territory', 'bruno', "
        "'2026-01-01T00:00:00+00:00', '2999-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    return db


def test_a_legacy_db_lacks_the_territory_column(legacy_db: Path) -> None:
    conn = sqlite3.connect(legacy_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fences)")}
    conn.close()
    assert "territory" not in cols  # the premise, not a test of our code


def test_opening_a_legacy_db_migrates_it(legacy_db: Path) -> None:
    assert store.schema_version(legacy_db) == store._SCHEMA_VERSION
    conn = sqlite3.connect(legacy_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fences)")}
    conn.close()
    assert "territory" in cols


def test_a_legacy_row_survives_the_migration(legacy_db: Path) -> None:
    """Migration must never destroy a rule someone is relying on."""
    rows = store.all_live_fences(legacy_db)
    assert len(rows) == 1
    assert rows[0]["rule"] == "legacy rule"
    assert rows[0]["territory"] == ""  # pre-existing rows are unscoped, not fenced-out


def test_a_legacy_row_still_evaluates_as_a_global_fence(legacy_db: Path) -> None:
    from hermes_fence import fences
    got = fences.evaluate(legacy_db, tool_name="terminal", agent="anyone",
                          territory="/var/home/bruno/repos/NEXUS")
    assert got["action"] == "block"
    assert "legacy rule" in got["message"]


def test_migration_is_idempotent(legacy_db: Path) -> None:
    for _ in range(3):
        store.open_db(legacy_db)
    assert store.schema_version(legacy_db) == store._SCHEMA_VERSION
    assert len(store.all_live_fences(legacy_db)) == 1


def test_migrating_twice_does_not_duplicate_the_column(legacy_db: Path) -> None:
    store.open_db(legacy_db)
    conn = sqlite3.connect(legacy_db)
    names = [r[1] for r in conn.execute("PRAGMA table_info(fences)")]
    conn.close()
    assert names.count("territory") == 1


def test_a_fresh_db_is_already_current(tmp_path: Path) -> None:
    db = store.open_db(tmp_path / "new.db")
    assert store.schema_version(db) == store._SCHEMA_VERSION


def test_migration_survives_a_second_process_holding_the_file(tmp_path: Path) -> None:
    """WAL means another session may have the db open; adding a column is still safe."""
    db = store.open_db(tmp_path / "shared.db")
    other = sqlite3.connect(db, timeout=5.0)
    other.execute("SELECT COUNT(*) FROM fences").fetchone()
    store.add_fence(db, tool_pattern="terminal", rule="r", reason="x",
                    territory="/tmp/.*", expires_at="2999-01-01T00:00:00+00:00")
    other.close()
    assert store.schema_version(db) == store._SCHEMA_VERSION
