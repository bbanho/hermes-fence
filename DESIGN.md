# hermes-fence — mini hooks with expiry, a stacked broadcast, and fences (not walls)

## The three primitives

| Primitive | What it is | Where it lives |
|---|---|---|
| **mini hook** | a small standing obligation/rule with a **vencimento** (expiry) and a trigger | `hooks` table |
| **broadcast** | a LIFO stack of messages **shared by every session and profile**, each session notified once | `broadcasts` + `broadcast_reads` |
| **fence** | a *bound* on agent territory — transparent, attributed, expiring, escalatable | `fences` table |

The thesis: **cercas entre agentes, não muros opacos.** A fence names itself, names its owner,
states the rule, and says what would satisfy it. It never silently denies.

## Architecture decisions (each verified against Hermes source, not memory)

| Decision | Source of truth |
|---|---|
| Ship as a **pip plugin** via entry-point group `hermes_agent.plugins` | The Hermes venv is shared by every profile, so one install reaches all profiles. `hermes_cli/plugins_loader.py:468` |
| Plugins are **opt-in** per config (`plugins.enabled`) | `hermes_cli/plugins_discovery.py:213` — a plugin not listed is skipped. This *is* the fence at the config layer |
| Notify via **`pre_llm_call` → `{"context": …}`** | `agent/turn_context.py:663` — context is appended to the **user message, never the system prompt**, so prompt caching is never broken |
| Shared store at the **canonical root**, not per-profile home | `hermes_constants.get_default_hermes_root()` returns `~/.hermes` even when `HERMES_HOME=~/.hermes/profiles/<name>`. One stack, all profiles. |
| Fences evaluated on **`pre_tool_call`** | `hermes_cli/plugins.py:1782` — returns `{"action":"block","message":…}`; the message *becomes the tool result*, so the agent always learns why |
| Escalation uses **`{"action":"approve"}`** | same site — routes to the human approval gate instead of a dead end |

## Why no daemon

Expiry is **lazy**: `expires_at` is swept on every read. A dead mini hook costs nothing when
nothing touches it, and there is no cron job to babysit. The same sweep retires broadcasts.
This is deliberate — a scheduled reaper for expiry is infrastructure nobody owns.

## Stack discipline

Append-only with a monotonic `seq`. Delivery is **oldest-first to each session, exactly once**
(`broadcast_reads`), unwinding the stack per reader. A session that never reads does not block
the stack; a session that reads twice does not get it twice.

## Revision: sync-on-intent, opt-in locks, and a mux (Sep 2026)

The three primitives above are **push**. The revision adds **pull** and **exclusion**, which
is what makes many agents survivable rather than merely possible.

### 1. Sync is a cursor, not a snapshot

The load-bearing invariant: *an agent syncs the delta since its own last read, never the
current state of everyone else.*

```
agent_state(agent, seq, intent, payload, updated_at)   -- PK (agent, seq), monotonic per agent
sync_cursors(reader, agent, last_seq)                  -- PK (reader, agent)
```

Steady-state cost is then ~zero: an idle agent's state produces no tokens. Without the
cursor this design collapses — "sync on nearly every intention" × N agents × full state is
just an expensive, distributed system prompt. The cursor is the invariant, not an
optimization.

### 2. Opt-in *is* the multi-step filter — no classification

"quase todas as intenções multi-step" is the right instinct about *when sync matters* and
the wrong mechanism for *detecting it*. Do not burn a model call per turn guessing whether a
turn is multi-step.

An agent that opts into syncing syncs on its own turn boundary. The opt-in *is* the filter:
for an opted-in agent, multi-step is the common case by construction, so no classifier is
needed and the cost is zero. The hook that sees the turn boundary is `pre_llm_call` — the
same channel that already carries the notices.

### 3. A lock is not a fence

Different verbs, different enforcement points, different failure modes. Keep them apart.

| | Fence | Lock |
|---|---|---|
| Verb | *you may not* | *I am* |
| Says | policy: rule, owner, reason, vencimento | possession: resource, holder, vencimento |
| Enforced at | `pre_tool_call` | `pre_tool_call` (contention check) |
| Failure mode | a wall (if unexplained) | a race (if not honored) |
| Relief | `hermes fence relax` — renegotiate | `hermes lock release` — or wait out the vencimento |

A lock is **advisory with a vencimento**: unreleased locks die on their own, because a dead
lock is a wall in disguise. Partial unique index on `resource` where `released_at IS NULL`
enforces at most one live holder per resource at the storage layer, not by convention.

```
locks(id, resource, holder, acquired_at, expires_at, released_at, note)
CREATE UNIQUE INDEX ux_live_lock ON locks(resource) WHERE released_at IS NULL;
```

### 4. Mux: a closed recipient grammar, not a string match

1xN is the degenerate case. Recipients are a parsed set with explicit precedence:

| Token | Meaning |
|---|---|
| `all` | every reader |
| `agent:<name>` / `profile:<name>` | that reader |
| `!<token>` | exclusion — **exclusions always win** |

Delivered iff the reader matches the set **and** has not read `seq`. Exactly-once is
unchanged: `broadcast_reads(reader, seq)` stays the sole dedup ledger, so a message addressed
to 50 readers is still delivered exactly once to each.

### 5. The bound that keeps the mux from leaking context

An unexpiring `all` broadcast is an unbounded context injection over weeks. Two mitigations,
both required:

- **Durable** broadcasts (no vencimento) must be addressed to a bounded set, or
- the store keeps a sticky window: `all` is capped at the newest N per reader, older entries
  fall out of the working set but stay in the table for audit.

`pending_broadcasts(limit=…)` already enforces the per-turn cap; the sticky window is what
makes the cap meaningful across turns instead of per call.

## Non-goals

- Not a policy language. One trigger per hook, one rule per fence.
- Not a scheduler. Time is an *input*, not an event.
- Not a memory system. The store is operational state with an audit trail, not knowledge.
- Not a distributed consensus system. Locks are advisory and expiring by design; if a
  partition could make two agents believe they hold the same lock, the answer is the
  vencimento, not a protocol.
