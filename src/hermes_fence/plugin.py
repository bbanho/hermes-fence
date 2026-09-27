"""The Hermes plugin: where fence state becomes agent behaviour.

Three hook points, each chosen for a specific reason verified in the Hermes source:

* ``pre_llm_call``   — one-shot context injected into the **user message**, never the system
  prompt (``agent/turn_context.py:663``). This is the notice channel: mini hooks, broadcast
  deliveries, and the sync delta ride here, so prompt caching is never invalidated.
* ``pre_tool_call``  — the only hook whose block directive is honored
  (``hermes_cli/plugins.py:1782``); its ``message`` becomes the tool result, which is exactly
  how a fence explains itself instead of silently denying.
* ``on_session_start`` — sweeps lapsed state so a store nobody has touched in a week comes
  back clean without a cron job.

The store path resolves to the **canonical Hermes root**, not the active profile's home, so
one stack is genuinely shared by every profile on the host.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from . import fences, locks, store, sync
from .mux import render_notice

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-fence"
DEFAULT_STICKY_WINDOW = 10
DEFAULT_NOTICE_LIMIT = 12


def db_path() -> Path:
    """The shared store: canonical root, not the active profile's home.

    ``get_default_hermes_root()`` resolves ``HERMES_HOME=<root>/profiles/<name>`` back to
    ``<root>``. A per-profile store would make "shared stack" a fiction.
    """
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except Exception:  # pragma: no cover - standalone/test use
        import os
        root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    path = Path(root) / "hermes-fence" / "fence.db"
    store.open_db(path)
    return path


def _setting(ctx: Any, key: str, default: Any) -> Any:
    try:
        val = ctx.get_config(key, None)
    except Exception:
        return default
    return default if val is None else val


def _agent_name(ctx: Any) -> str:
    """Who is *acting* — the profile. Fences and locks are scoped to this."""
    try:
        name = ctx.profile_name
    except Exception:
        return ""
    return (name or "").strip() or "default"


def _reader_id(ctx: Any, session_id: str = "") -> str:
    """Who is *listening* — a session, qualified by the agent that owns it.

    Two independent axes, and the recipient grammar needs both:

    * **session** so a broadcast is delivered once per session (two sessions of the same
      profile each get their own copy — a stack that is "notified in all sessions");
    * **agent** so ``agent:infra`` can address a specific agent across every one of its
      sessions. A Hermes session id carries no profile name, so the agent has to be part of
      the reader key; ``agent:`` matching therefore cannot rely on the raw session id.

    Format ``<agent>|<session_id>``; the agent half is what the mux matches on.
    """
    agent = _agent_name(ctx)
    return f"{agent}|{(session_id or '').strip()}"


def _sync_peers(db: Path, ctx: Any) -> list[str]:
    """Peers to sync, from opt-in config. An empty list means: sync nothing."""
    raw = _setting(ctx, "sync_peers", [])
    if isinstance(raw, str):
        raw = [t for t in raw.replace(",", " ").split() if t]
    peers = [str(p).strip() for p in (raw or []) if str(p).strip()]
    if "*" in peers or "all" in peers:
        return [a for a in sync.known_agents(db) if a != _agent_name(ctx)]
    return peers


def _notices(db: Path, *, reader: str, agent: str, ctx: Any) -> str:
    """Assemble everything this turn should know: hooks, broadcast, sync delta.

    *reader* is the session (delivery is per-session); *agent* is the profile (fences,
    locks, and peer identity are per-agent). Mini hooks are per-agent standing obligations,
    so they are listed for the agent; the broadcast and sync delta are per-reader.
    """
    blocks: list[str] = []

    hooks = store.list_hooks(db, agent=agent)
    if hooks:
        lines = [f"[MINI HOOKS — {len(hooks)} standing obligation(s)]"]
        for h in hooks:
            owner = f" (set by {h['owner']})" if h["owner"] else ""
            window = f" [lifts {h['expires_at']}]" if h["expires_at"] else ""
            lines.append(f"  - {h['text']}{owner}{window}")
        blocks.append("\n".join(lines))

    window_size = int(_setting(ctx, "sticky_window", DEFAULT_STICKY_WINDOW))
    delivered = store.pending_broadcasts(
        db, reader=reader, limit=DEFAULT_NOTICE_LIMIT, sticky_window=window_size
    )
    if delivered:
        blocks.append(render_notice(delivered))

    peers = _sync_peers(db, ctx)
    if peers:
        delta = sync.sync(db, reader=agent, agents=peers)
        if delta:
            lines = [f"[PEER STATE — delta since your last sync ({len(delta)} change(s))]"]
            for e in delta:
                payload = e["payload"]
                detail = f" {payload}" if payload else ""
                lines.append(f"  {e['agent']} · {e['intent']}{detail}")
            blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def on_pre_llm_call(ctx: Any, *, session_id: str = "", platform: str = "", **_: Any) -> Optional[dict]:
    """Inject this turn's notices. Returning ``{"context": ...}`` keeps the system prompt byte-stable."""
    try:
        db = db_path()
        text = _notices(db, reader=_reader_id(ctx, session_id), agent=_agent_name(ctx), ctx=ctx)
        return {"context": text} if text else None
    except Exception as exc:  # never break a turn because the notice layer hiccuped
        logger.warning("hermes-fence pre_llm_call failed: %s", exc, exc_info=True)
        return None


def on_pre_tool_call(ctx: Any, *, tool_name: str = "", args: Optional[dict] = None,
                     **_: Any) -> Optional[dict]:
    """Enforce fences on this tool call, as an explanation rather than a denial."""
    try:
        db = db_path()
        agent = _agent_name(ctx)
        decision = fences.evaluate(db, tool_name=tool_name, agent=agent)
        if decision["action"] == "block":
            return {"action": "block", "message": decision["message"]}
        if decision["aware_of"] and _setting(ctx, "announce_boundaries", True):
            return {
                "action": "modify",
                "args": {},
                "context": (f"Note: `{tool_name}` carries fences for other agents: "
                            + ", ".join(decision["aware_of"])),
            }
    except Exception as exc:
        logger.warning("hermes-fence pre_tool_call failed: %s", exc, exc_info=True)
    return None


def on_session_start(ctx: Any, *, session_id: str = "", **_: Any) -> None:
    """Sweep lapsed state on session start — the only 'cron' this system has."""
    try:
        store.sweep_expired(db_path())
    except Exception as exc:
        logger.warning("hermes-fence session sweep failed: %s", exc)


def _cli(args: Any) -> int:
    from .cli import main
    return main(args)


def register(ctx: Any) -> None:
    """Entry point called by Hermes plugin discovery."""
    ctx.register_hook("pre_llm_call", lambda **kw: on_pre_llm_call(ctx, **kw))
    ctx.register_hook("pre_tool_call", lambda **kw: on_pre_tool_call(ctx, **kw))
    ctx.register_hook("on_session_start", lambda **kw: on_session_start(ctx, **kw))

    def _setup(sub: Any) -> None:
        sub.add_argument("action", nargs="?", default="status")
        sub.add_argument("text", nargs="*", default=[])

    def _handler(args: Any) -> int:
        return _cli(args)

    ctx.register_cli_command(
        "fence", "Mini hooks, broadcast stack, locks, and fences (cercas, not walls)",
        _setup, _handler,
        description=(
            "fence hook add|list|recall · fence say (broadcast) · fence listen · "
            "fence lock|unlock|locks · fence put (fence) · fence relax · fence status"
        ),
    )
