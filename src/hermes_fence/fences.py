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
from .store import scope_contains
from .territory import matches as _territory_matches, repo_root as _repo_root

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


def evaluate(
    db: Path, *, tool_name: str, agent: str, territory: str = "",
) -> dict[str, Any]:
    """Decide what happens to one tool call.

    Returns ``{"action": "allow"|"block"|"warn", "message": str, "aware_of": [rules]}``.

    Two kinds of neighbour exist, and conflating them is the bug this module was corrected for:

    * **binding** — a fence whose territory *and* scope both admit this caller. It blocks.
    * **advisory** — a fence on a sibling worktree of the same repo, or scoped to another
      agent. It only warns. Several differently-named agents share one workdir here, so "not
      my fence" must never read as "nobody is fenced".
    """
    mine = store.fences_for(db, tool_name=tool_name, agent=agent, territory=territory)
    mine_ids = {f["id"] for f in mine}
    my_root = _repo_root(territory) if territory else ""

    advisory = [
        f["rule"] for f in store.fences_for_tool(db, tool_name=tool_name)
        if f["id"] not in mine_ids and _is_advisory(f, agent, territory, my_root)
    ]

    if not mine:
        # No "warn" action: `pre_tool_call` honors only `block` and `approve` (verified in
        # hermes_cli/plugins.py:_get_pre_tool_call_directive_details). An advisory is still
        # reported, but it does not change the verdict — the caller decides what to do with it.
        return {"action": "allow", "message": "", "aware_of": advisory}

    notice = "\n\n".join(render_fence_notice(f) for f in mine)
    return {
        "action": "block",
        "message": f"Blocked by fence — not a wall, a bound you can renegotiate:\n\n{notice}",
        "aware_of": advisory,
    }


def _is_advisory(fence: dict[str, Any], agent: str, territory: str, my_root: str) -> bool:
    """Whether a non-binding fence is still worth telling this caller about.

    Three triggers, each grounded in how this host actually runs:

    * a **sibling worktree** — the fence names another checkout that contains, or is contained
      by, the checkout this caller is standing in. They share one ``.git`` and one push target,
      so this agent should know; blocking it would be opacity with extra steps.
    * a **parent tree** — the fence names a tree that contains a nested worktree we are in.
    * **another agent's rule on a tree we are also in** — scope names agents other than us.

    Deliberately *not* "any fence anywhere": a fence in an unrelated repo is noise, and noise
    is how a warning stops being read.
    """
    pattern = fence.get("territory") or ""
    if pattern:
        if not territory or not my_root:
            return False
        base = pattern.removesuffix(".*").rstrip("/")
        if _territory_matches(pattern, territory):
            # It nominally matches us but did not bind: a nested checkout. Still advisory.
            return True
        # Our own checkout sits inside the fenced tree (or vice versa) — same repo, adjacent.
        return _same_repo(base, my_root)
    # No territory given, so it applies wherever its agents are — including here, if we are
    # one of them. A scope that excludes us is the only remaining advisory case.
    return bool(fence.get("scope")) and not scope_contains(fence["scope"], agent)


def _same_repo(a: str, b: str) -> bool:
    """Whether two canonical paths are the same checkout or nested inside one another."""
    if not a or not b:
        return False
    if a == b:
        return True
    return a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def render_advisory(fence: dict[str, Any]) -> str:  # pragma: no cover - formatting helper
    return f"{fence.get('rule')} (set by {fence.get('owner') or 'unattributed'})"
