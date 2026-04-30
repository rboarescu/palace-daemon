# `GET /graph` endpoint + `list_tunnels` workaround

> **Status:** Proposed (this PR). Sections below are the design notes
> + the `list_tunnels` discrepancy write-up that the `/graph` handler
> in `main.py` references.
>
> Verification on a 151K-drawer palace: `/graph` returns 200 in ~34s
> on the first hit (cold), <1s warm, full payload (36 wings, 68 rooms,
> 9 tunnels, 6 KG entities, 3 KG triples). SME-style adapter
> composition over MCP took ~5 minutes against the same palace under
> typical load — `/graph` is ~430× faster end-to-end.

## Context

`multipass-structural-memory-eval` (SME, JP's fork) is shipping a
`MemPalaceDaemonAdapter` that talks to palace-daemon over HTTP for
SME's structural diagnostics (Cat 4 / 5 / 8 / 9). The adapter ships
working today against daemon 1.5.1 by walking four MCP tools
(`mempalace_list_wings`, `mempalace_list_rooms` per wing,
`mempalace_list_tunnels`, `mempalace_kg_query`), but that path is slow
over HTTP — `list_wings` takes ~30s on the 151K-drawer palace, and
`list_rooms` × N wings serialised over HTTP is painful.

Adding a single `GET /graph` endpoint makes structural snapshots fast
(parallel-gather server-side) and removes the only reason SME has to
walk MCP for a structural read. The shape mirrors `/stats` exactly
— a thin asyncio.gather over a few MCP tools, plus a direct sqlite
read of the KG (the daemon already owns the file, so a parallel KG
read does not violate the single-writer invariant).

This was extracted from the SME-side spec at:
`multipass-structural-memory-eval/docs/superpowers/specs/2026-04-25-mempalace-daemon-adapter-design.md`

The SME-side adapter does NOT block on this — it falls back to MCP
when `/graph` 404s. This work is purely a performance + correctness
upgrade for the daemon path.

**Coordination note:** SME committed a coordination note that this
endpoint is JP-driven, separate session/PR. Land at your own cadence;
SME will start preferring `/graph` after a daemon version bump (1.6.0).

---

## Part 1 — `GET /graph` endpoint

Add to `palace-daemon/main.py`, mirroring the `/stats` handler's
`asyncio.gather` pattern (the `@app.get("/stats")` route just above
where `/graph` slots in).

### Response shape

```json
{
  "wings": {"<wing_name>": <drawer_count>, ...},
  "rooms": [
    {"wing": "<wing_name>", "rooms": {"<room_name>": <drawer_count>, ...}},
    ...
  ],
  "tunnels": [
    {"room": "<room_name>", "wings": ["<wing_a>", "<wing_b>", ...]},
    ...
  ],
  "kg_entities": [
    {"id": "<id>", "name": "<name>", "type": "<type>", "properties": {...}},
    ...
  ],
  "kg_triples": [
    {
      "subject": "<id>",
      "predicate": "<predicate>",
      "object": "<id>",
      "valid_from": "<iso8601>",
      "valid_to": "<iso8601 or null>",
      "confidence": <float>,
      "source_file": "<path>"
    },
    ...
  ],
  "kg_stats": {"entities": <int>, "triples": <int>}
}
```

### Implementation sketch

```python
@app.get("/graph")
async def graph(x_api_key: str | None = Header(default=None)):
    _check_auth(x_api_key)

    def call(tool, args):
        return _call({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })

    # Phase 1: parallel-gather the once-per-palace tools
    wings_resp, tunnels_resp, kg_stats_resp = await asyncio.gather(
        call("mempalace_list_wings", {}),
        call("mempalace_list_tunnels", {}),
        call("mempalace_kg_stats", {}),
    )
    wings_payload = _unwrap(wings_resp) or {}
    wings = wings_payload.get("wings") or {}

    # Phase 2: parallel list_rooms per wing
    room_responses = await asyncio.gather(*[
        call("mempalace_list_rooms", {"wing": w}) for w in wings
    ])
    rooms = [
        {"wing": w, "rooms": (_unwrap(r) or {}).get("rooms", {})}
        for w, r in zip(wings, room_responses)
    ]

    # Phase 3: KG entities + triples via direct sqlite read
    kg_entities, kg_triples = _read_kg_direct()

    return {
        "wings": wings,
        "rooms": rooms,
        "tunnels": _unwrap(tunnels_resp) or [],
        "kg_entities": kg_entities,
        "kg_triples": kg_triples,
        "kg_stats": _unwrap(kg_stats_resp) or {},
    }
```

### `_read_kg_direct()` helper

Read-only SQLite read of `~/.mempalace/knowledge_graph.sqlite3` from
inside the daemon process. The daemon already owns the palace file
group, so this does not introduce a new writer — the KG is a separate
DB file from the ChromaDB persistent client.

```python
import sqlite3
from pathlib import Path

KG_PATH = Path("~/.mempalace/knowledge_graph.sqlite3").expanduser()


def _read_kg_direct() -> tuple[list[dict], list[dict]]:
    if not KG_PATH.exists():
        return [], []
    try:
        conn = sqlite3.connect(
            f"file:{KG_PATH}?mode=ro", uri=True, timeout=5
        )
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return [], []

    entities: list[dict] = []
    triples: list[dict] = []
    try:
        try:
            for r in conn.execute(
                "SELECT id, name, type, properties FROM entities"
            ):
                import json as _json
                try:
                    props = _json.loads(r["properties"] or "{}")
                except Exception:
                    props = {}
                entities.append({
                    "id": r["id"],
                    "name": r["name"],
                    "type": r["type"] or "unknown",
                    "properties": props,
                })
        except sqlite3.OperationalError:
            pass
        try:
            for r in conn.execute(
                "SELECT subject, predicate, object, valid_from, valid_to, "
                "confidence, source_file FROM triples"
            ):
                triples.append({
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                    "confidence": r["confidence"],
                    "source_file": r["source_file"],
                })
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return entities, triples
```

### Auth

Same `_check_auth(x_api_key)` pattern as every other endpoint. No
special permissions — `/graph` is read-only.

---

## Part 2 — Fix `mempalace_list_tunnels` inconsistency

Live observation 2026-04-25 against the 151K-drawer palace at
`disks.jphe.in:8085`:

- `GET /stats` → `"graph": {"tunnel_rooms": 9, ...}`
- `POST /mcp { "name": "mempalace_list_tunnels" }` → `[]`

The two should agree on what "a tunnel" is. Most likely cause: the
list_tunnels tool's implementation has drifted from the
`mempalace_graph_stats` tool that `/stats` consumes (different schema
queries, stale cache, or a guard-condition that filtered to zero in
the new MCP path). Investigation steps:

1. Identify the file/function backing `mempalace_list_tunnels` in the
   `mempalace` package (likely under `mempalace/mcp_tools/` or
   `mempalace/palace_graph.py` — recent inspection is needed because
   mempalace is a third-party install in pipx venv).
2. Compare the predicate it uses to detect tunnels against the
   predicate `mempalace_graph_stats` uses for `tunnel_rooms`.
3. Reconcile so both produce the same set. The "tunnel = room shared
   by ≥2 wings" definition is the historical one — anything else is
   a regression.
4. Add a regression test that asserts:
   `len(list_tunnels()) == graph_stats()["tunnel_rooms"]`.

If the bug is in `mempalace` itself (third-party), file an issue
upstream OR shadow the implementation inside palace-daemon's
`/graph` endpoint. The `/graph` response should always agree with
`/stats.tunnel_rooms`.

---

## Part 3 — Tests

### Daemon-side unit test

`palace-daemon/tests/test_graph_endpoint.py`:

- Mock `_call` to return canned `mempalace_list_wings` /
  `mempalace_list_tunnels` / `mempalace_kg_stats` /
  `mempalace_list_rooms` envelopes.
- Mock `_read_kg_direct` to return canned tuples.
- Assert response shape, asyncio.gather is used (rooms-per-wing
  happens in parallel — test by counting elapsed time vs. serial
  baseline, or by mocking `_call` with a small `asyncio.sleep` and
  asserting total elapsed < N × sleep duration).

### Live smoke

Once deployed:

```bash
set -a; source ~/.config/palace-daemon/env; set +a
curl -sS --max-time 60 -H "X-API-Key: $PALACE_API_KEY" \
    "$PALACE_DAEMON_URL/graph" | python3 -m json.tool | head -50
```

Expected: 2026-04-25 the live palace has 36 wings, ~9 tunnels, and a
small KG — full response in well under 30s (vs. ~30s for `list_wings`
alone on the slow path).

### SME-side acceptance

After daemon ships 1.6.0, SME's
`tests/test_mempalace_daemon_integration.py` will start exercising
the fast path automatically (the adapter prefers `/graph` by
default). No SME-side change needed at deploy time.

---

## Part 4 — Version bump + release

1. `palace-daemon/main.py` `VERSION` constant → `1.6.0`
2. `CHANGELOG.md`:
   ```
   ## 1.6.0 — 2026-XX-XX
   - Added `GET /graph` endpoint: structural snapshot in one call,
     consumed by SME's `MemPalaceDaemonAdapter` fast path.
   - Fixed `mempalace_list_tunnels` returning [] while
     `mempalace_graph_stats.tunnel_rooms > 0`.
   ```
3. Deploy to disks
   (`disks.jphe.in:8085`).
4. Notify SME side — adapter starts using `/graph` automatically; the
   MCP fallback stays in place for upstream forks / older daemons.

---

## Out of scope for this work

- Streaming `/drawers` endpoint for SME drawer-level projection. SME's
  current spec accepts the coarser snapshot (no per-drawer entities,
  no `same_file` sibling edges) for the daemon path. If a future SME
  category needs drawer-level surface, add a streaming endpoint in
  a separate PR.
- Vector-search bug under `kind=content`: SME observed that
  `q=memory&kind=content` returns
  `vector search unavailable: Error executing plan: Internal error:
  Error finding id` while `q=hello&kind=all` works fine. That's a
  separate bug — likely a code path that combines the kind filter
  and vector search under specific scope conditions. Not covered
  here. SME's adapter surfaces these warnings into `QueryResult.error`
  as `WARN: ...` so Cat 9 can score them — the framework is the
  right place to characterise the gap, the daemon is the right place
  to fix it.
