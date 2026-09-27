"""`hermes fence` — the operator surface.

Deliberately one verb tree, because the primitives share a store and a reader identity. Every
subcommand prints what it changed and what the reader will now see, so the system is
inspectable from the terminal without opening SQLite.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from . import fences, locks, store, sync
from .fences import render_fence_notice
from .mux import parse_recipients


def _expiry(value: str | None) -> str | None:
    """Accept ``30m``, ``6h``, ``2d``, an ISO stamp, or nothing (= never)."""
    if not value:
        return None
    v = value.strip()
    if v in {"-", "never", "none"}:
        return None
    units = {"m": "minutes", "h": "hours", "d": "days"}
    if len(v) > 1 and v[-1].lower() in units and v[:-1].isdigit():
        delta = timedelta(**{units[v[-1].lower()]: int(v[:-1])})
        return (datetime.now(timezone.utc) + delta).isoformat()
    return v  # assume ISO; the store treats unparsable as "trust me until then"


def _rest(args: Any) -> list[str]:
    return [str(a) for a in (getattr(args, "text", None) or [])]


def main(args: Any) -> int:
    action = (getattr(args, "action", "status") or "status").strip()
    rest = _rest(args)
    from .plugin import db_path
    db = db_path()

    handlers = {
        "status": _status, "hook": _hook, "say": _say, "listen": _listen,
        "lock": _lock, "unlock": _unlock, "locks": _locks, "put": _put,
        "relax": _relax, "fences": _fences, "publish": _publish, "sweep": _sweep,
    }
    fn = handlers.get(action)
    if fn is None:
        print(f"unknown action: {action}\n{_usage()}")
        return 2
    return fn(db, rest, args)


def _usage() -> str:
    return (
        "hermes fence <action>\n"
        "  status                          sweep + counts\n"
        "  hook add|list|recall <text>     mini hooks (--ttl 6h)\n"
        "  say <text>                      broadcast (--to 'all !agent:x', --ttl 1h)\n"
        "  listen                          drain the stack for this session\n"
        "  lock <resource>                 take possession (--ttl 30m)\n"
        "  unlock <resource>               release it\n"
        "  locks                           who holds what\n"
        "  put <rule>                      raise a fence (--tool, --agent, --ttl, --why)\n"
        "  relax <id>                      release a fence, keeping the record\n"
        "  fences                          show every live fence\n"
        "  publish <intent>                publish peer state for sync\n"
        "  sweep                           expire lapsed rows now"
    )


def _opt(args: Any, *names: str, default: str = "") -> str:
    for n in names:
        v = getattr(args, n, None)
        if v:
            return str(v)
    return default


def _status(db: Any, rest: list[str], args: Any) -> int:
    counts = store.sweep_expired(db)
    print(f"store: {db}")
    print(f"retired this sweep: {counts}")
    print(f"mini hooks live:   {len(store.list_hooks(db))}")
    print(f"broadcasts total:  {len(store.broadcast_history(db, limit=999))}")
    print(f"locks live:        {len(locks.live_locks(db))}")
    print(f"fences live:       {len(store.list_fences(db))}")
    print(f"agents published:  {', '.join(sync.known_agents(db)) or '(none)'}")
    return 0


def _hook(db: Any, rest: list[str], args: Any) -> int:
    sub = rest[0] if rest else "list"
    if sub == "add":
        text = " ".join(rest[1:]).strip()
        if not text:
            print("hook add needs text"); return 2
        hid = store.add_hook(db, text=text, owner=_opt(args, "owner", default="bruno"),
                             expires_at=_expiry(_opt(args, "ttl", "expires_at")))
        print(f"mini hook #{hid} until {_expiry(_opt(args, 'ttl', 'expires_at')) or 'never'}")
        return 0
    if sub == "recall":
        if len(rest) < 2:
            print("hook recall needs an id"); return 2
        print("recalled" if store.recall_hook(db, int(rest[1])) else "no such hook")
        return 0
    for h in store.list_hooks(db):
        print(f"#{h['id']} {h['text']}  (by {h['owner'] or '—'}"
              f"{', lifts ' + h['expires_at'] if h['expires_at'] else ', no expiry'})")
    return 0


def _say(db: Any, rest: list[str], args: Any) -> int:
    text = " ".join(rest).strip()
    if not text:
        print("say needs text"); return 2
    to = _opt(args, "to", default="all")
    parse_recipients(to)  # fail loudly here rather than at delivery
    seq = store.push_broadcast(db, text=text, author=_opt(args, "author", default="bruno"),
                               to=to, expires_at=_expiry(_opt(args, "ttl", "expires_at")))
    print(f"#{seq} -> {to}")
    return 0


def _listen(db: Any, rest: list[str], args: Any) -> int:
    reader = _opt(args, "reader", default=_default_reader())
    rows = store.pending_broadcasts(db, reader=reader,
                                    limit=int(_opt(args, "limit", default="20") or 20),
                                    sticky_window=int(_opt(args, "sticky_window", default="10") or 10))
    if not rows:
        print("stack empty")
        return 0
    for r in rows:
        print(f"#{r['seq']} {r['author'] or '—'}: {r['text']}")
    return 0


def _lock(db: Any, rest: list[str], args: Any) -> int:
    if not rest:
        print("lock needs a resource"); return 2
    got = locks.acquire(db, resource=rest[0], holder=_opt(args, "holder", default=_default_reader()),
                        expires_at=_expiry(_opt(args, "ttl", "expires_at")),
                        note=_opt(args, "note"))
    if got["granted"]:
        print(f"holding {rest[0]} (#{got['lock_id']}) until {got['expires_at'] or 'never'}")
        return 0
    print(f"DENIED — {got['message']}")
    return 1


def _unlock(db: Any, rest: list[str], args: Any) -> int:
    if not rest:
        print("unlock needs a resource"); return 2
    ok = locks.release(db, resource=rest[0], holder=_opt(args, "holder", default=_default_reader()),
                       note=_opt(args, "note"))
    print("released" if ok else "not held by you")
    return 0 if ok else 1


def _locks(db: Any, rest: list[str], args: Any) -> int:
    for row in locks.live_locks(db):
        print(f"#{row['id']} {row['resource']} held by {row['holder']}"
              f" until {row['expires_at'] or 'never'}")
    return 0


def _put(db: Any, rest: list[str], args: Any) -> int:
    rule = " ".join(rest).strip()
    if not rule:
        print("put needs a rule"); return 2
    scope = {}
    if _opt(args, "agent"):
        scope["agent"] = _opt(args, "agent")
    try:
        fid = store.add_fence(db, tool_pattern=_opt(args, "tool", default=".*"),
                              scope=scope, rule=rule, reason=_opt(args, "why", "reason"),
                              owner=_opt(args, "owner", default="bruno"),
                              expires_at=_expiry(_opt(args, "ttl", "expires_at")))
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    fence = store.get_fence(db, fid)
    print(f"fence #{fid} raised:\n{render_fence_notice(fence or {})}")
    return 0


def _relax(db: Any, rest: list[str], args: Any) -> int:
    if not rest:
        print("relax needs a fence id"); return 2
    if store.relax_fence(db, int(rest[0]), note=_opt(args, "note")):
        print(f"fence #{rest[0]} released (row kept on record)")
        return 0
    print("no such fence")
    return 1


def _fences(db: Any, rest: list[str], args: Any) -> int:
    for f in store.list_fences(db):
        print(render_fence_notice(f)); print()
    return 0


def _publish(db: Any, rest: list[str], args: Any) -> int:
    seq = sync.publish(db, agent=_opt(args, "agent", default=_default_reader()),
                       intent=" ".join(rest) or "state",
                       expires_at=_expiry(_opt(args, "ttl", "expires_at")))
    print(f"published seq={seq}")
    return 0


def _sweep(db: Any, rest: list[str], args: Any) -> int:
    print(store.sweep_expired(db))
    return 0


def _default_reader() -> str:
    """Best-effort identity for CLI use; sessions inject their own."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        import os
        return os.environ.get("HERMES_PROFILE", "default")
