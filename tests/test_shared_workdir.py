"""The correction this project was rebuilt for: **the unit of contention is a path.**

Six repos under ~/repos carry multiple git worktrees sharing one ``.git``. Agents are not
separate places — several differently-named agents sit in the *same* checkout, and one agent
holds two sessions on one branch. Scoping by agent name sees neither collision. These tests
hold the path-based semantics in place.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_fence import fences, store
from hermes_fence.territory import canonical


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    """A repo with a *real* worktree, mirroring ~/repos/NEXUS on this host.

    The worktree carries a ``.git`` **file** (gitlink), not a directory — that is what
    ``git worktree add`` produces, and it is what makes the nested checkout a separate
    territory rather than a subdirectory of the main one.
    """
    main = tmp_path / "NEXUS"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    wt = main / ".worktrees" / "t_abc"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/t_abc\n")
    return main


# --- the case that motivated the rewrite -----------------------------------

def test_a_territory_fence_binds_every_agent_in_that_tree(shared: Path) -> None:
    """Two differently-named agents, one checkout: the fence binds BOTH.

    Scoping by agent name would have bound neither, because neither name is in the scope.
    """
    db = store.open_db(shared / "f.db")
    main = canonical(str(shared))
    store.add_fence(
        db, tool_pattern="terminal", territory=f"{main}/.*",
        rule="no force push to feat/unification", reason="3 worktrees share one branch",
        owner="bruno", expires_at=_future(),
    )
    assert fences.evaluate(db, tool_name="terminal", agent="infra", territory=main)["action"] == "block"
    assert fences.evaluate(db, tool_name="terminal", agent="nexus", territory=main)["action"] == "block"
    assert fences.evaluate(db, tool_name="terminal", agent="saude", territory=main)["action"] == "block"


def test_a_territory_fence_does_not_bind_a_different_tree(tmp_path: Path) -> None:
    other = tmp_path / "odonto-agenda"
    other.mkdir()
    db = store.open_db(tmp_path / "f.db")
    store.add_fence(
        db, tool_pattern="terminal", territory=f"{canonical(str(other))}/.*",
        rule="no deploy on friday", reason="release window", owner="bruno",
        expires_at=_future(),
    )
    assert fences.evaluate(db, tool_name="terminal", agent="infra", territory=str(other))["action"] == "block"
    assert fences.evaluate(db, tool_name="terminal", agent="infra", territory="/somewhere/else")["action"] == "allow"


def test_a_sibling_worktree_is_advised_not_bound(shared: Path) -> None:
    """Same repo, different checkout: adjacent, so a warning — never a block.

    They share one `.git`, so this agent should know; but blocking it would be the same
    opacity as an unscoped wall, just with more steps.
    """
    db = store.open_db(shared / "f.db")
    main = canonical(str(shared))
    sibling = canonical(str(shared / ".worktrees" / "t_abc"))
    store.add_fence(
        db, tool_pattern="terminal", territory=f"{main}/.*",
        rule="main worktree is being rebased", reason="in-flight rebase", owner="bruno",
        expires_at=_future(),
    )
    got = fences.evaluate(db, tool_name="terminal", agent="infra", territory=sibling)
    assert got["action"] == "allow"
    assert "main worktree is being rebased" in got["aware_of"]


def test_a_territory_and_agent_scope_compose(shared: Path) -> None:
    """Both dimensions must admit the caller; a fence naming only one is not narrower than needed."""
    db = store.open_db(shared / "f.db")
    main = canonical(str(shared))
    store.add_fence(
        db, tool_pattern="write_file", territory=f"{main}/.*", scope={"agent": "infra"},
        rule="infra owns migrations", reason="only infra knows the schema", owner="bruno",
        expires_at=_future(),
    )
    assert fences.evaluate(db, tool_name="write_file", agent="infra", territory=main)["action"] == "block"
    assert fences.evaluate(db, tool_name="write_file", agent="nexus", territory=main)["action"] == "allow"


def test_agent_scope_alone_still_works_for_a_global_rule(shared: Path) -> None:
    db = store.open_db(shared / "f.db")
    main = canonical(str(shared))
    store.add_fence(
        db, tool_pattern="terminal", scope={"agent": "infra"},
        rule="infra publishes releases", reason="only infra holds the deploy key", owner="bruno",
        expires_at=_future(),
    )
    assert fences.evaluate(db, tool_name="terminal", agent="infra", territory=main)["action"] == "block"
    assert fences.evaluate(db, tool_name="terminal", agent="nexus", territory=main)["action"] == "allow"


def test_a_territory_fence_still_names_its_escape_hatch(shared: Path) -> None:
    db = store.open_db(shared / "f.db")
    main = canonical(str(shared))
    store.add_fence(
        db, tool_pattern="terminal", territory=f"{main}/.*", rule="no force push",
        reason="shared branch", owner="bruno", expires_at=_future(),
    )
    msg = fences.evaluate(db, tool_name="terminal", agent="infra", territory=main)["message"]
    assert "no force push" in msg
    assert "hermes fence relax" in msg          # negotiable
    assert "bruno" in msg                        # attributable
    assert _future()[:10] in msg or "lifts" in msg  # has a vencimento


# --- the guard against a too-broad fence ------------------------------------

def test_a_bare_wildcard_territory_is_refused(shared: Path) -> None:
    db = store.open_db(shared / "f.db")
    with pytest.raises(ValueError, match="bind every path"):
        store.add_fence(db, tool_pattern="terminal", territory="*", rule="nothing at all",
                        reason="oops", expires_at=_future())


def test_a_territory_fence_without_a_reason_is_refused(shared: Path) -> None:
    db = store.open_db(shared / "f.db")
    with pytest.raises(ValueError, match="reason"):
        store.add_fence(db, tool_pattern="terminal", territory="/repos/.*", rule="no push")


def test_a_trailing_wildcard_on_a_real_tree_is_accepted(shared: Path) -> None:
    """`/repos/NEXUS/.*` is the legitimate way to fence a whole checkout."""
    db = store.open_db(shared / "f.db")
    fid = store.add_fence(db, tool_pattern="terminal", territory=f"{canonical(str(shared))}/.*",
                          rule="no rebase", reason="shared checkout", expires_at=_future())
    assert store.get_fence(db, fid)["territory"] == f"{canonical(str(shared))}/.*"


# --- the same workdir, no fence: what prevents the collision ----------------

def test_two_agents_in_one_checkout_contend_for_one_lock(shared: Path) -> None:
    """Locks are already resource-scoped, so a shared workdir collides correctly by path."""
    from hermes_fence import locks
    db = store.open_db(shared / "f.db")
    first = locks.acquire(db, resource="repo:NEXUS", holder="infra", expires_at=_future())
    second = locks.acquire(db, resource="repo:NEXUS", holder="nexus", expires_at=_future())
    assert first["granted"] is True
    assert second["granted"] is False
    assert "infra" in second.get("held_by", "")
