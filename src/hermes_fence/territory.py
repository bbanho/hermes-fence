"""Territory: the real unit of contention.

**Correction (Sep 2026).** The first cut scoped fences, locks, and hooks by *agent name*, which
is the wrong unit. In this environment agents are not separate places — they are **several
processes sharing the same workdir**, and six repos carry multiple worktrees that share one
`.git`. Two agents named differently can be writing the same file at the same instant; one
agent can hold two sessions on the same branch. The name tells you nothing about who will
collide. **The path does.**

So the identity model gains a third axis and promotes one to first class:

| Axis | Answers | Binds |
|---|---|---|
| `territory` (path) | *where* — the git worktree / workdir | fences, locks, hotspot warnings |
| `agent` (profile) | *who* is acting | who set a rule, lock holder, sync peers |
| `session` (id) | *which* conversation | broadcast delivery (exactly once) |

`territory` is derived, never declared: the effective cwd of the tool call, canonicalized
(resolved symlinks, no trailing slash) so two agents in ``NEXUS`` and
``NEXUS/.worktrees/t_x`` are correctly *different* territories — they are different checkouts,
and fencing them together would be the same opacity in the other direction.

A fence is scoped to a territory and an **operation**, not to a tool name. ``write_file`` in
``NEXUS`` and ``write_file`` in ``odonto-agenda`` are different acts; the tool name alone is
too coarse to be a meaningful boundary.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

# A territory is a path, not an identity. Reject a pattern that binds the whole host — but
# NOT one that names a specific tree with a trailing wildcard, which is the legitimate way to
# fence an entire checkout (e.g. ``/repos/NEXUS/.*``). Only a bare wildcard is too broad.
_TOO_BROAD = re.compile(r"^(\*|\.\*)$")


def canonical(path: str | os.PathLike[str]) -> str:
    """Canonical form of a territory: absolute, symlinks resolved, no trailing slash.

    ``resolve()`` on a non-existent path still normalizes, so a fence can name a directory
    that does not exist yet. ``strict=False`` is the default and is what we want.
    """
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        return str(Path(str(path)).expanduser())


def _parent_of_file(path: str) -> str:
    """A file path's territory is the directory holding it.

    ``write_file(path=…/src/main.rs)`` acts on a file, but the boundary that matters is the
    tree around it — two agents editing different files in one checkout collide anyway.
    """
    return canonical(str(Path(canonical(path)).parent))


def from_tool_args(tool_name: str, args: Optional[dict[str, Any]]) -> str:
    """Best-effort territory for one tool call.

    Resolution order, most specific first:

    1. an explicit ``workdir``/``cwd``/``path`` argument — the caller named the place;
    2. the live scoped terminal cwd (``agent.runtime_cwd.scope_terminal_cwd``) — this is what
       a bare ``terminal`` call actually runs in, and it is the value that differs between
       worktrees even though the process cwd is identical;
    3. the process cwd, as a last resort.

    Deliberately *not* read from the hook payload: ``pre_tool_call`` does not carry cwd, and a
    fence that guessed the process cwd would bind every worktree of a repo at once — exactly
    the opacity this project exists to avoid.
    """
    if isinstance(args, dict):
        # A path naming a FILE belongs to the tree containing it; a directory names itself.
        for key in ("workdir", "cwd", "directory", "dir"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return canonical(val)
        val = args.get("path")
        if isinstance(val, str) and val.strip():
            return _parent_of_file(val)
    if tool_name in {"terminal", "process", "process_manage"}:
        try:
            from agent.runtime_cwd import scope_terminal_cwd
            scoped = (scope_terminal_cwd() or "").strip()
            if scoped:
                return canonical(scoped)
        except Exception:
            pass
    try:
        return canonical(os.getcwd())
    except OSError:  # pragma: no cover - defensive
        return ""


def repo_root(territory: str) -> str:
    """The checkout a territory belongs to, or the territory itself if untracked.

    Pure filesystem walk — no ``git`` subprocess. This runs on the hot path (every tool call),
    and spawning a process to answer "where am I" would cost more than the fence it informs.

    A worktree's ``.git`` is a *file* pointing at the real gitdir, so both forms are accepted.
    """
    cur = Path(territory) if territory else None
    while cur and cur != cur.parent:
        if (cur / ".git").exists():
            return canonical(str(cur))
        cur = cur.parent
    return territory


def _crosses_a_checkout(base: str, territory: str) -> bool:
    """Whether *territory* is a different checkout nested inside *base*.

    A worktree nested under a main checkout (``repo/.worktrees/t_x``) is path-wise inside its
    parent but is a *separate* checkout with its own index and branch. A fence on the parent
    must not bind it: that would make a boundary silently wider than the operator declared.

    Checks the territory itself first — a real worktree carries a ``.git`` **file**, while a
    main checkout carries a ``.git`` directory, so the presence test must accept both.
    """
    if territory == base or not territory.startswith(base.rstrip("/") + "/"):
        return False
    if not (Path(territory) / ".git").exists():
        return False
    return True


def pattern_is_too_broad(pattern: str) -> bool:
    """Whether a territory pattern would match the entire host.

    ``*`` or ``.*`` as a whole pattern is almost always an author mistake, and a fence that
    binds everything is a wall wearing a fence's clothes.
    """
    return not pattern or bool(_TOO_BROAD.match(pattern.strip()))


def matches(territory_pattern: str, territory: str) -> bool:
    """Regex match of a territory pattern against a canonical path.

    A trailing ``.*`` means *this tree and everything inside it* — which includes the tree
    root. Treating it as a plain regex would exclude the root and silently leave a checkout
    unfenced while every subdirectory of it was fenced, which is the exact shape of an
    invisible boundary.

    An unparsable pattern degrades to a literal comparison rather than to "match nothing" or,
    far worse, to "match everything".
    """
    if not territory_pattern:
        return False
    if territory_pattern.endswith(".*"):
        base = territory_pattern[:-2].rstrip("/")
        if territory == base:
            return True
    try:
        return re.fullmatch(territory_pattern, territory) is not None
    except re.error:
        return territory_pattern == territory
