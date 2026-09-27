"""Fence negotiation: a bound is delivered as an explanation, never as a silent denial."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_fence import store
from hermes_fence.fences import render_fence_notice, evaluate


def _future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return store.open_db(tmp_path / "fence.db")


# --- a fence states what it is and what would satisfy it -------------------

def test_notice_names_the_rule_the_owner_and_the_reason(db: Path) -> None:
    store.add_fence(
        db, tool_pattern="terminal", scope={"agent": "infra"},
        rule="no force push", reason="two agents raced one branch", owner="bruno",
        expires_at=_future(),
    )
    fence = store.fences_for(db, tool_name="terminal", agent="infra")[0]
    notice = render_fence_notice(fence)
    assert "no force push" in notice
    assert "bruno" in notice
    assert "raced" in notice


def test_notice_always_carries_an_escape_hatch(db: Path) -> None:
    """A fence that cannot be relieved is a wall. Every notice says how to lift it."""
    store.add_fence(
        db, tool_pattern="terminal", scope={}, rule="no force push",
        reason="branch race", owner="bruno", expires_at=_future(),
    )
    notice = render_fence_notice(store.fences_for(db, tool_name="terminal", agent="x")[0])
    assert "hermes fence relax" in notice


def test_notice_reveals_the_vencimento(db: Path) -> None:
    """Fences are temporary by construction; the agent can see when the bound lifts."""
    expires = _future(hours=6)
    store.add_fence(db, tool_pattern="terminal", scope={}, rule="r", reason="x",
                    owner="o", expires_at=expires)
    notice = render_fence_notice(store.fences_for(db, tool_name="terminal", agent="x")[0])
    assert "2026" in notice or "+0" in notice or expires[:10] in notice


# --- evaluation: allow, block-with-reason, or escalate ---------------------

def test_unfenced_tool_is_allowed(db: Path) -> None:
    decision = evaluate(db, tool_name="terminal", agent="nexus")
    assert decision["action"] == "allow"


def test_fenced_tool_is_blocked_with_an_explanation(db: Path) -> None:
    store.add_fence(db, tool_pattern="terminal", scope={}, rule="no force push",
                    reason="branch race", owner="bruno", expires_at=_future())
    decision = evaluate(db, tool_name="terminal", agent="nexus")
    assert decision["action"] == "block"
    assert "no force push" in decision["message"]


def test_evaluate_exposes_the_escape_hatch_to_the_agent(db: Path) -> None:
    store.add_fence(db, tool_pattern="terminal", scope={}, rule="r", reason="x",
                    owner="o", expires_at=_future())
    message = evaluate(db, tool_name="terminal", agent="nexus")["message"]
    assert "hermes fence relax" in message


def test_expired_fence_lets_the_tool_through(db: Path) -> None:
    store.add_fence(db, tool_pattern="terminal", scope={}, rule="r", reason="x",
                    expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert evaluate(db, tool_name="terminal", agent="nexus")["action"] == "allow"


def test_most_specific_fence_wins(db: Path) -> None:
    store.add_fence(db, tool_pattern="terminal", scope={}, rule="global rule", reason="x",
                    expires_at=_future())
    store.add_fence(db, tool_pattern="terminal", scope={"agent": "infra"},
                    reason="race", rule="infra rule", expires_at=_future())
    decision = evaluate(db, tool_name="terminal", agent="infra")
    assert "infra rule" in decision["message"]


def test_fence_outside_scope_does_not_bind(db: Path) -> None:
    store.add_fence(db, tool_pattern="terminal", scope={"agent": "infra"}, reason="race",
                    rule="infra rule", expires_at=_future())
    assert evaluate(db, tool_name="terminal", agent="nexus")["action"] == "allow"


def test_allow_decision_still_lists_the_boundary(db: Path) -> None:
    """Even when a tool passes, the agent learns a fence exists on it — cercas are visible."""
    store.add_fence(db, tool_pattern="terminal", scope={"agent": "infra"}, reason="race",
                    rule="infra rule", expires_at=_future())
    decision = evaluate(db, tool_name="terminal", agent="nexus")
    assert decision["action"] == "allow"
    assert decision["aware_of"] == ["infra rule"]
