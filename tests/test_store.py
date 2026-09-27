"""Store contracts: expiry is lazy, the stack is shared, reads are exactly-once."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_fence import store


def _future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return store.open_db(tmp_path / "fence.db")


# --- expiry is lazy and sweep-based ---------------------------------------

def test_expired_hooks_are_invisible_without_a_daemon(db: Path) -> None:
    store.add_hook(db, text="dead", expires_at=_past(), owner="a")
    assert store.list_hooks(db) == []


def test_live_hooks_are_listed(db: Path) -> None:
    store.add_hook(db, text="live", expires_at=_future(), owner="a")
    assert [h["text"] for h in store.list_hooks(db)] == ["live"]


def test_hooks_without_expiry_never_expire(db: Path) -> None:
    store.add_hook(db, text="permanent", expires_at=None, owner="a")
    assert len(store.list_hooks(db)) == 1


def test_sweep_retires_expired_rows_but_keeps_the_audit_trail(db: Path) -> None:
    hid = store.add_hook(db, text="dead", expires_at=_past(), owner="a")
    store.sweep_expired(db)
    assert store.get_hook(db, hid) is not None  # row survives for audit
    assert store.list_hooks(db) == []  # but is never delivered


def test_expiry_beats_recall(db: Path) -> None:
    """A hook past its vencimento is dead even if explicitly recalled."""
    hid = store.add_hook(db, text="dead", expires_at=_past(), owner="a")
    store.recall_hook(db, hid)
    assert store.list_hooks(db) == []


# --- the stack is shared across every session and profile ------------------

def test_broadcasts_are_delivered_oldest_first(db: Path) -> None:
    first = store.push_broadcast(db, text="one", expires_at=_future())
    second = store.push_broadcast(db, text="two", expires_at=_future())
    got = store.pending_broadcasts(db, reader="session-A")
    assert [g["seq"] for g in got] == [first, second]


def test_a_message_is_delivered_exactly_once_per_session(db: Path) -> None:
    store.push_broadcast(db, text="once", expires_at=_future())
    assert len(store.pending_broadcasts(db, reader="session-A")) == 1
    assert store.pending_broadcasts(db, reader="session-A") == []


def test_one_session_draining_does_not_starve_another(db: Path) -> None:
    """A stack read is per-reader: draining session-A must not consume session-B's copy."""
    store.push_broadcast(db, text="shared", expires_at=_future())
    assert store.pending_broadcasts(db, reader="session-A")
    assert len(store.pending_broadcasts(db, reader="session-B")) == 1


def test_draining_a_reader_unwinds_the_stack_in_order(db: Path) -> None:
    for i in range(4):
        store.push_broadcast(db, text=f"m{i}", expires_at=_future())
    got = store.pending_broadcasts(db, reader="session-A")
    assert [g["text"] for g in got] == ["m0", "m1", "m2", "m3"]


def test_expired_broadcasts_are_not_delivered(db: Path) -> None:
    store.push_broadcast(db, text="stale", expires_at=_past())
    assert store.pending_broadcasts(db, reader="session-A") == []


def test_push_broadcast_records_monotonic_sequence(db: Path) -> None:
    a = store.push_broadcast(db, text="a", expires_at=None)
    b = store.push_broadcast(db, text="b", expires_at=None)
    assert b > a


def test_durable_broadcast_without_expiry_never_dies(db: Path) -> None:
    """Durable + bounded address: no vencimento, never culled.

    A durable + *unbounded* (`all`) message is a different case — it needs a sticky window
    or it is an unbounded context leak, and that is pinned in test_locks_sync_mux.py.
    """
    store.push_broadcast(db, text="permanent", to="agent:infra", expires_at=None)
    assert len(store.pending_broadcasts(db, reader="infra")) == 1


# --- fences are bounds, and bounds are attributable ------------------------

def test_fence_is_invisible_outside_its_scope(db: Path) -> None:
    store.add_fence(
        db, tool_pattern="terminal", scope={"agent": "infra"}, reason="two agents raced one branch",
        rule="no force push", expires_at=_future(),
    )
    assert store.fences_for(db, tool_name="terminal", agent="nexus") == []
    assert store.fences_for(db, tool_name="terminal", agent="infra")


def test_fence_tool_pattern_is_a_regex(db: Path) -> None:
    store.add_fence(db, tool_pattern=r"write_(file|patch)", scope={}, rule="no raw writes",
                    expires_at=_future())
    assert store.fences_for(db, tool_name="write_file", agent="x")
    assert store.fences_for(db, tool_name="read_file", agent="x") == []


def test_expired_fence_releases_its_territory(db: Path) -> None:
    store.add_fence(db, tool_pattern="terminal", scope={}, rule="stale", expires_at=_past())
    assert store.fences_for(db, tool_name="terminal", agent="x") == []


def test_fence_reports_its_owner_and_rule_so_it_is_never_opaque(db: Path) -> None:
    store.add_fence(
        db, tool_pattern="terminal", scope={"agent": "infra"},
        rule="no force push", reason="two agents raced the same branch",
        expires_at=_future(), owner="bruno",
    )
    fence = store.fences_for(db, tool_name="terminal", agent="infra")[0]
    assert fence["rule"] == "no force push"
    assert fence["owner"] == "bruno"
    assert "raced" in fence["reason"]


def test_a_fence_can_be_relaxed_without_deleting_the_row(db: Path) -> None:
    """Fences are negotiable: a fence is a *binding*, and the row keeps the negotiation trail."""
    fid = store.add_fence(db, tool_pattern="terminal", scope={}, rule="no force push",
                          expires_at=_future())
    store.relax_fence(db, fid, note="main is green again")
    assert store.fences_for(db, tool_name="terminal", agent="x") == []
    assert store.get_fence(db, fid)["rule"] == "no force push"  # still on record
