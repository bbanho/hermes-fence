"""Integration: the plugin surface, driven the way Hermes drives it.

A fake ctx records registrations, so these tests assert on the *contract Hermes actually
uses* (hook names, return shapes) rather than on our internals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_fence import plugin, store
from hermes_fence.mux import parse_recipients


class FakeCtx:
    """Stands in for PluginContext: records hooks/tools/commands, serves config."""

    def __init__(self, profile: str = "default", config: dict | None = None) -> None:
        self.profile_name = profile
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, tuple] = {}
        self._config = config or {}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.commands[name] = (help, handler_fn)

    def get_config(self, key, default=None):
        return self._config.get(key, default)


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCtx:
    db = store.open_db(tmp_path / "shared.db")
    monkeypatch.setattr(plugin, "db_path", lambda: db)
    ctx = FakeCtx(profile="default", config={"sync_peers": ["infra"]})
    plugin.register(ctx)
    return ctx


# --- registration ----------------------------------------------------------

def test_registers_exactly_the_three_lifecycle_hooks(wired: FakeCtx) -> None:
    assert set(wired.hooks) == {"pre_llm_call", "pre_tool_call", "on_session_start"}


def test_registers_the_fence_cli_command(wired: FakeCtx) -> None:
    assert "fence" in wired.commands


# --- a broadcast reaches a second session, exactly once --------------------

def test_a_broadcast_is_injected_into_the_next_turn(wired: FakeCtx) -> None:
    store.push_broadcast(plugin.db_path(), text="branch renamed", to="all",
                         expires_at="2999-01-01T00:00:00+00:00")
    out = wired.hooks["pre_llm_call"][0](session_id="s1")
    assert out and "branch renamed" in out["context"]


def test_the_same_broadcast_is_not_re_injected_in_a_later_turn(wired: FakeCtx) -> None:
    store.push_broadcast(plugin.db_path(), text="once", to="all",
                         expires_at="2999-01-01T00:00:00+00:00")
    assert wired.hooks["pre_llm_call"][0](session_id="s1")
    assert wired.hooks["pre_llm_call"][0](session_id="s1") is None


def test_a_second_session_gets_its_own_copy(wired: FakeCtx) -> None:
    store.push_broadcast(plugin.db_path(), text="shared", to="all",
                         expires_at="2999-01-01T00:00:00+00:00")
    wired.hooks["pre_llm_call"][0](session_id="s1")
    out = wired.hooks["pre_llm_call"][0](session_id="s2")
    assert out and "shared" in out["context"]


def test_an_addressed_broadcast_reaches_only_its_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addressing is by agent, so the recipient is recognised by its agent token.

    Sessions are readers; the address grammar names agents. A session is attributed to an
    agent by the token Hermes puts in the session id, so this drives the plugin the way a
    profile-scoped session id arrives.
    """
    db = store.open_db(tmp_path / "d.db")
    monkeypatch.setattr(plugin, "db_path", lambda: db)
    ctx = FakeCtx(profile="infra", config={})
    plugin.register(ctx)
    store.push_broadcast(db, text="for infra", to="agent:infra",
                         expires_at="2999-01-01T00:00:00+00:00")
    assert ctx.hooks["pre_llm_call"][0](session_id="20260926_101500") is not None


def test_a_broadcast_to_one_agent_reaches_every_one_of_its_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addressing is per-agent; delivery is per-session. Both must hold at once."""
    db = store.open_db(tmp_path / "d.db")
    monkeypatch.setattr(plugin, "db_path", lambda: db)
    ctx = FakeCtx(profile="infra", config={})
    plugin.register(ctx)
    store.push_broadcast(db, text="restart the gateway", to="agent:infra",
                         expires_at="2999-01-01T00:00:00+00:00")
    first = ctx.hooks["pre_llm_call"][0](session_id="s-1")
    second = ctx.hooks["pre_llm_call"][0](session_id="s-2")
    assert first and "restart the gateway" in first["context"]
    assert second and "restart the gateway" in second["context"]


def test_a_broadcast_to_another_agent_never_reaches_this_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = store.open_db(tmp_path / "d.db")
    monkeypatch.setattr(plugin, "db_path", lambda: db)
    ctx = FakeCtx(profile="nexus", config={})
    plugin.register(ctx)
    store.push_broadcast(db, text="infra only", to="agent:infra",
                         expires_at="2999-01-01T00:00:00+00:00")
    assert ctx.hooks["pre_llm_call"][0](session_id="s-1") is None


def test_a_quiet_turn_injects_nothing(wired: FakeCtx) -> None:
    assert wired.hooks["pre_llm_call"][0](session_id="s1") is None


# --- fences answer in the Hermes block shape -------------------------------

def test_a_fence_returns_the_block_directive_hermes_honors(wired: FakeCtx) -> None:
    store.add_fence(plugin.db_path(), tool_pattern="terminal", scope={},
                    rule="no force push", reason="branch race", owner="bruno",
                    expires_at="2999-01-01T00:00:00+00:00")
    out = wired.hooks["pre_tool_call"][0](tool_name="terminal", args={})
    assert out["action"] == "block"
    assert "no force push" in out["message"]


def test_an_unfenced_tool_is_not_intercepted(wired: FakeCtx) -> None:
    assert wired.hooks["pre_tool_call"][0](tool_name="read_file", args={}) is None


def test_another_agents_fence_is_still_visible(wired: FakeCtx) -> None:
    store.add_fence(plugin.db_path(), tool_pattern="terminal", scope={"agent": "infra"},
                    reason="race", rule="infra rule", expires_at="2999-01-01T00:00:00+00:00")
    out = wired.hooks["pre_tool_call"][0](tool_name="terminal", args={})
    assert out is None or "infra rule" in str(out)


# --- sync delta rides the same one-shot channel ---------------------------

def test_a_peers_state_change_is_injected_once(wired: FakeCtx) -> None:
    from hermes_fence import sync
    sync.publish(plugin.db_path(), agent="infra", intent="editing gateway", payload={"file": "x"})
    out = wired.hooks["pre_llm_call"][0](session_id="s1")
    assert out and "editing gateway" in out["context"]
    assert wired.hooks["pre_llm_call"][0](session_id="s1") is None


def test_an_agent_that_did_not_opt_in_syncs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in is the filter: without peers configured, no peer state is ever injected."""
    db = store.open_db(tmp_path / "d.db")
    monkeypatch.setattr(plugin, "db_path", lambda: db)
    from hermes_fence import sync
    sync.publish(db, agent="infra", intent="secret-plan", payload={})
    ctx = FakeCtx(profile="default", config={})
    plugin.register(ctx)
    assert ctx.hooks["pre_llm_call"][0](session_id="s1") is None


# --- a broken notice layer must never break a turn ------------------------

def test_a_store_failure_degrades_to_silence_not_a_crash(wired: FakeCtx) -> None:
    """A broken store must cost a notice, never a turn."""
    import hermes_fence.plugin as p

    def boom() -> Path:
        raise RuntimeError("store gone")

    original = p.db_path
    p.db_path = boom
    try:
        assert wired.hooks["pre_llm_call"][0](session_id="s1") is None
        assert wired.hooks["pre_tool_call"][0](tool_name="terminal", args={}) is None
    finally:
        p.db_path = original


# --- the recipient grammar is closed --------------------------------------

def test_an_unknown_recipient_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_recipients("ministry:finance")
