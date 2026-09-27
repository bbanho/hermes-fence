"""Locks (possession), sync (pull-by-cursor), and the mux (addressed broadcast).

Three rules these tests hold sacred:
* a lock that is not renewed **dies on its own** — a dead lock is a wall in disguise;
* sync is a **delta since the reader's own cursor**, never a snapshot of everyone;
* an exclusion always beats an inclusion, and delivery stays exactly-once.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_fence import locks, store, sync
from hermes_fence.mux import parse_recipients


def _future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return store.open_db(tmp_path / "fence.db")


# --- locks: possession with a vencimento -----------------------------------

def test_an_uncontended_lock_is_granted(db: Path) -> None:
    assert locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future())["granted"]


def test_a_live_lock_held_elsewhere_denies_the_second_holder(db: Path) -> None:
    locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future())
    second = locks.acquire(db, resource="repo:NEXUS", holder="nexus", expires_at=_future())
    assert second["granted"] is False
    assert second["held_by"] == "infra"


def test_the_holder_reacquiring_its_own_lock_is_allowed(db: Path) -> None:
    """Re-entrancy is not contention; an agent must be able to re-assert its own claim."""
    locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future())
    assert locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future())["granted"]


def test_an_expired_lock_releases_its_resource_without_a_sweeper(db: Path) -> None:
    locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_past())
    assert locks.acquire(db, resource="repo:NEXUS", holder="nexus", expires_at=_future())["granted"]


def test_releasing_a_lock_yields_the_resource_immediately(db: Path) -> None:
    locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future())
    assert locks.release(db, resource="repo:NEXUS", holder="infra")
    assert locks.acquire(db, resource="repo:NEXUS", holder="nexus", expires_at=_future())["granted"]


def test_a_released_lock_stays_on_record(db: Path) -> None:
    lid = locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future())["lock_id"]
    locks.release(db, resource="repo:NEXUS", holder="infra", note="done")
    row = locks.get_lock(db, lid)
    assert row["released_at"] is not None
    assert row["note"] == "done"


def test_renewing_pushes_the_vencimento_out(db: Path) -> None:
    got = locks.acquire(db, resource="r", holder="a", expires_at=_future(hours=1))
    locks.renew(db, got["lock_id"], holder="a", expires_at=_future(hours=6))
    assert locks.get_lock(db, got["lock_id"])["expires_at"] > _future(hours=2)


def test_a_denial_states_the_vencimento_so_the_waiter_can_time_its_retry(db: Path) -> None:
    locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future(hours=2))
    denial = locks.acquire(db, resource="repo:NEXUS", holder="nexus", expires_at=_future())
    assert denial["expires_at"] is not None
    assert "wait" in denial["message"].lower()


# --- sync: the delta since the reader's own cursor -------------------------

def test_a_first_sync_reports_published_state(db: Path) -> None:
    sync.publish(db, agent="infra", intent="planning", payload={"step": 1})
    got = sync.sync(db, reader="nexus", agents=["infra"])
    assert [e["intent"] for e in got] == ["planning"]


def test_a_second_sync_from_the_same_reader_returns_nothing_new(db: Path) -> None:
    """The cursor is the whole point: an unchanged peer costs zero tokens."""
    sync.publish(db, agent="infra", intent="planning", payload={})
    sync.sync(db, reader="nexus", agents=["infra"])
    assert sync.sync(db, reader="nexus", agents=["infra"]) == []


def test_a_second_reader_gets_the_same_delta_independently(db: Path) -> None:
    sync.publish(db, agent="infra", intent="planning", payload={})
    sync.sync(db, reader="nexus", agents=["infra"])
    assert len(sync.sync(db, reader="health", agents=["infra"])) == 1


def test_sync_returns_only_entries_after_the_cursor(db: Path) -> None:
    sync.publish(db, agent="infra", intent="a", payload={})
    first = sync.sync(db, reader="nexus", agents=["infra"])
    sync.publish(db, agent="infra", intent="b", payload={})
    sync.publish(db, agent="infra", intent="c", payload={})
    later = sync.sync(db, reader="nexus", agents=["infra"])
    assert [e["intent"] for e in later] == ["b", "c"]
    assert first[0]["intent"] == "a"


def test_sync_is_scoped_to_the_agents_requested(db: Path) -> None:
    sync.publish(db, agent="infra", intent="infra-step", payload={})
    sync.publish(db, agent="health", intent="health-step", payload={})
    got = sync.sync(db, reader="nexus", agents=["infra"])
    assert [e["intent"] for e in got] == ["infra-step"]


def test_an_idle_peer_contributes_nothing(db: Path) -> None:
    sync.publish(db, agent="infra", intent="a", payload={})
    sync.sync(db, reader="nexus", agents=["infra", "health"])
    assert sync.sync(db, reader="nexus", agents=["infra", "health"]) == []


def test_expired_peer_state_is_not_synced(db: Path) -> None:
    sync.publish(db, agent="infra", intent="stale", payload={}, expires_at=_past())
    assert sync.sync(db, reader="nexus", agents=["infra"]) == []


def test_publish_replaces_the_latest_intent_rather_than_appending_noise(db: Path) -> None:
    """State is a current value per (agent, intent key), not a log of every restatement."""
    sync.publish(db, agent="infra", intent="planning", payload={"step": 1})
    sync.publish(db, agent="infra", intent="planning", payload={"step": 2})
    got = sync.sync(db, reader="nexus", agents=["infra"])
    assert len(got) == 1
    assert got[0]["payload"] == {"step": 2}


def test_sync_does_not_move_a_cursor_for_an_agent_it_was_not_asked_about(db: Path) -> None:
    sync.publish(db, agent="infra", intent="a", payload={})
    sync.publish(db, agent="health", intent="b", payload={})
    sync.sync(db, reader="nexus", agents=["infra"])
    got = sync.sync(db, reader="nexus", agents=["health"])
    assert [e["intent"] for e in got] == ["b"]


# --- the mux: addressed broadcast -------------------------------------------

def test_all_addresses_every_reader(db: Path) -> None:
    store.push_broadcast(db, text="hi", to="all", expires_at=_future())
    assert store.pending_broadcasts(db, reader="anyone") != []


def test_a_named_recipient_reaches_only_itself(db: Path) -> None:
    store.push_broadcast(db, text="hi", to="agent:infra", expires_at=_future())
    assert store.pending_broadcasts(db, reader="health") == []
    assert len(store.pending_broadcasts(db, reader="infra")) == 1


def test_many_recipients_reach_each_exactly_once(db: Path) -> None:
    store.push_broadcast(db, text="hi", to="agent:infra agent:health profile:nexus", expires_at=_future())
    for reader in ("infra", "health", "nexus", "finance"):
        first = len(store.pending_broadcasts(db, reader=reader))
        second = len(store.pending_broadcasts(db, reader=reader))
        assert second == 0
        assert first == (1 if reader != "finance" else 0)


def test_an_exclusion_beats_inclusion(db: Path) -> None:
    store.push_broadcast(db, text="hi", to="all !agent:health", expires_at=_future())
    assert store.pending_broadcasts(db, reader="health") == []
    assert len(store.pending_broadcasts(db, reader="infra")) == 1


def test_a_sole_exclusion_yields_nothing(db: Path) -> None:
    store.push_broadcast(db, text="hi", to="!agent:infra", expires_at=_future())
    assert store.pending_broadcasts(db, reader="infra") == []


def test_addresses_may_be_comma_separated(db: Path) -> None:
    store.push_broadcast(db, text="hi", to="agent:infra,agent:health", expires_at=_future())
    assert len(store.pending_broadcasts(db, reader="health")) == 1


def test_an_unparseable_address_is_rejected_rather_than_guessed(db: Path) -> None:
    with pytest.raises(ValueError):
        parse_recipients("everyone")


def test_a_durable_all_broadcast_is_capped_into_a_sticky_window(db: Path) -> None:
    """An unexpiring `all` message is unbounded context; the window is what stops the leak."""
    for i in range(10):
        store.push_broadcast(db, text=f"m{i}", to="all", expires_at=None)
    got = store.pending_broadcasts(db, reader="nexus", sticky_window=3)
    assert len(got) == 3
    assert [g["text"] for g in got] == ["m7", "m8", "m9"]  # newest N


def test_a_durable_broadcast_to_a_bounded_set_is_never_capped(db: Path) -> None:
    for i in range(10):
        store.push_broadcast(db, text=f"m{i}", to="agent:infra", expires_at=None)
    assert len(store.pending_broadcasts(db, reader="infra", sticky_window=3)) == 10


def test_an_expired_addressed_message_is_not_delivered(db: Path) -> None:
    store.push_broadcast(db, text="stale", to="all", expires_at=_past())
    assert store.pending_broadcasts(db, reader="nexus") == []
