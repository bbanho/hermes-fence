"""Fence evaluation and the wording of a bound.

The thesis in one place: **cercas entre agentes, não muros opacos.** A fence that fires
returns a message that names the rule, the owner, the reason, the vencimento, and how to
relieve it. A tool that passes a fence still learns the fence exists (``aware_of``), because
a boundary an agent cannot see is indistinguishable from an arbitrary one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import store

_ESCAPE = "hermes fence relax"


def _fmt_expiry(expires_at: str | None) -> str:
    return f"lifts {expires_at}" if expires_at else "no expiry set"


def render_fence_notice(fence: dict[str, Any]) -> str:
    """Human- and agent-readable statement of one bound."""
    parts = [
        f"FENCE ACTIVE on `{fence['tool_pattern']}`: {fence['rule']}",
        f"  reason:  {fence.get('reason') or '(none given)'}",
        f"  set by:  {fence.get('owner') or 'unattributed'}",
        f"  window:  {_fmt_expiry(fence.get('expires_at'))}",
        f"  scope:   {fence.get('scope') or 'all agents'}",
    ]
    if fence.get("id") is not None:
        parts.append(
            f"  relieve: `{_ESCAPE} {fence['id']} --note \"<why>\"` "
            f"(a fence is negotiable; the row stays on record)"
        )
    return "\n".join(parts)


def evaluate(db: Path, *, tool_name: str, agent: str) -> dict[str, Any]:
    """Decide what happens to one tool call.

    Returns ``{"action": "allow"|"block", "message": str, "aware_of": [rules]}``.
    ``aware_of`` lists fence rules bound to this tool for *other* agents — the boundary
    stays visible to everyone, so agents can negotiate before they collide.
    """
    mine = store.fences_for(db, tool_name=tool_name, agent=agent)
    mine_ids = {f["id"] for f in mine}
    others = [
        f["rule"] for f in store.fences_for_tool(db, tool_name=tool_name)
        if f["id"] not in mine_ids
    ]

    if not mine:
        return {"action": "allow", "message": "", "aware_of": others}

    notice = "\n\n".join(render_fence_notice(f) for f in mine)
    return {
        "action": "block",
        "message": f"Blocked by fence — not a wall, a bound you can renegotiate:\n\n{notice}",
        "aware_of": others,
    }
