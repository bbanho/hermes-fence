"""The mux: an addressed broadcast with a closed recipient grammar.

1xN is the degenerate case of a broadcast; a mux addresses **any combination** of readers.
Recipients are a *parsed* set, never a string match, and two rules hold unconditionally:

1. **Exclusions always beat inclusions.** ``all !agent:health`` reaches everyone but health.
2. **A token this module cannot parse is an error, never a silent "everyone".** Defaulting an
   unknown address to the widest set is how a typo becomes a leak.

Exactly-once delivery is unchanged and still owned by ``store.broadcast_reads``: one message
addressed to fifty readers is delivered once to each of them.
"""

from __future__ import annotations

from typing import Any

ALL = "all"
_KINDS = ("agent", "profile")
_SEP = "|"


def _agent_of(reader: str) -> str:
    """The agent half of a ``<agent>|<session_id>`` reader; the whole string if unqualified."""
    return reader.split(_SEP, 1)[0] if _SEP in reader else reader


class Recipients:
    """A parsed address set: an inclusion set minus an exclusion set."""

    __slots__ = ("include_all", "include", "exclude", "raw")

    def __init__(self, include_all: bool, include: frozenset[str],
                 exclude: frozenset[str], raw: str) -> None:
        self.include_all = include_all
        self.include = include
        self.exclude = exclude
        self.raw = raw

    def matches(self, reader: str) -> bool:
        """A reader matches if it or its agent half is addressed.

        Readers are ``<agent>|<session_id>`` so delivery stays per-session while addressing
        stays per-agent. A bare reader (no agent half) still matches an ``agent:`` address,
        which keeps the store usable from a plain CLI.
        """
        if reader in self.exclude or _agent_of(reader) in self.exclude:
            return False
        if self.include_all:
            return True
        return reader in self.include or _agent_of(reader) in self.include

    @property
    def is_bounded(self) -> bool:
        """Whether the set names specific readers — an unbounded ``all`` is cap-worthy."""
        return not self.include_all

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Recipients(all={self.include_all}, +{sorted(self.include)}, -{sorted(self.exclude)})"


def _parse_token(tok: str) -> tuple[str, str]:
    """``agent:infra`` -> ``("infra", "agent")``. Raises on anything outside the grammar."""
    tok = tok.strip()
    if not tok:
        raise ValueError("empty recipient token")
    if tok == ALL:
        return (ALL, ALL)
    kind, _, name = tok.partition(":")
    kind = kind.strip().lower()
    if kind not in _KINDS:
        raise ValueError(
            f"unknown recipient {tok!r}: expected 'all', 'agent:<name>', "
            f"'profile:<name>', or a '!'-prefixed exclusion"
        )
    name = name.strip()
    if not name:
        raise ValueError(f"recipient {tok!r} is missing a name after '{kind}:'")
    return (name, kind)


def parse_recipients(to: str) -> Recipients:
    """Parse a whitespace- or comma-separated address string.

    ``all`` and any explicit ``agent:``/``profile:`` token may appear; a leading ``!`` marks
    an exclusion. Mixing ``all`` with named includes is allowed (the union is taken) so a
    sender never has to know the roster.
    """
    include_all = False
    include: set[str] = set()
    exclude: set[str] = set()
    tokens = [t for t in to.replace(",", " ").split() if t]
    if not tokens:
        raise ValueError("a broadcast needs at least one recipient")
    for tok in tokens:
        is_exclusion = tok.startswith("!")
        name, kind = _parse_token(tok[1:] if is_exclusion else tok)
        if kind == ALL:
            # `!all` is a legal no-op (a caller building addresses dynamically can land on it),
            # not a syntax error: it simply matches nobody, so the message reaches nobody.
            if not is_exclusion:
                include_all = True
            continue
        if kind == "profile":
            # Profile and agent share one reader namespace on purpose: a session's reader id
            # is its profile name, so a profile address and an agent address are the same
            # thing to the delivery check. Kept as a distinct token for legibility at the call site.
            pass
        (exclude if is_exclusion else include).add(name)
    if not include_all and not include:
        # Both `!agent:x` alone and `!all` alone are legal-but-empty: a caller assembling
        # addresses dynamically can land on them, and "reaches nobody" is a well-defined,
        # harmless outcome — an error here would be a crash, not a safety gain.
        return Recipients(False, frozenset(), frozenset(exclude), to)
    return Recipients(include_all, frozenset(include), frozenset(exclude), to)


def render_notice(rows: list[dict[str, Any]], *, heading: str = "BROADCAST") -> str:
    """Format delivered messages as one bounded block for the turn's context injection."""
    lines = [f"[{heading}] {len(rows)} message(s) on the shared stack:"]
    for r in rows:
        author = r.get("author") or "unknown"
        stamp = (r.get("created_at") or "")[:19]
        lines.append(f"  #{r['seq']} {author} @{stamp}: {r['text']}")
    return "\n".join(lines)
