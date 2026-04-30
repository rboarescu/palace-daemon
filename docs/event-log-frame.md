# Palace-daemon as a materialized-view coordinator over an event log

## Thesis

mempalace is event-streaming-shaped, even if it hasn't been articulated that
way. The **conversation transcripts** are the immutable log. Everything
else — chroma vectors, the knowledge graph, wing/room categorization, AAAK
compressed text — is a **materialized view** computed from the log.

palace-daemon's role under this frame is the **view coordinator**: accept
writes to the log, serve queries against views, coordinate view rebuilds,
handle write fairness while a view is mid-rebuild. That role is durable
across any backend choice (ChromaDB, pgvector, AGE, anything else); the
backend just determines *how* a particular view is stored.

This document exists to articulate that frame, identify where the
existing daemon code already implements it implicitly, and clarify what
changes (and doesn't) under upcoming storage transitions.

## Background: the Kleppmann frame

Martin Kleppmann's _Designing Data-Intensive Applications_ and the talk
[Turning the database inside-out](https://www.confluent.io/blog/turning-the-database-inside-out-with-apache-samza/)
articulate a thesis worth restating here:

- The **log** of state changes is the source of truth.
- Everything else — the SQL database your app queries, the search
  index, the cache, the graph DB — is a **derived view** computed from
  that log.
- Schema changes become _drop the view, rebuild from log_.
- Adding a new query pattern becomes _compute a new view from the same log_.
- Failure recovery becomes _the log is fine, rebuild whichever view broke_.

This dissolves a lot of database orthodoxy about "primary storage" vs
"indexes" — they're the same kind of thing at different update latencies.

LinkedIn built Kafka and Samza as the infrastructure for this pattern at
their scale. Confluent commercialized it. The pattern itself is
generalizable to any system where you can identify an immutable log and
the views that derive from it.

## Mapping mempalace onto the frame

| Kleppmann concept | mempalace component |
|---|---|
| Immutable event log | Raw conversation transcripts + verbatim drawers in the chroma collection. The 96.6% LongMemEval result is _from this log_, not from any reformatted view. |
| Materialized view: semantic search | The **chroma vector index** (HNSW). |
| Materialized view: structured facts | The **knowledge graph** (`knowledge_graph.sqlite3` — entities + triples). |
| Materialized view: categorical structure | **Wings / rooms / halls** (encoded in metadata, surfaced by `mempalace_list_wings` / `mempalace_list_rooms` / `mempalace_list_tunnels` MCP tools). |
| Materialized view: token density | **AAAK compressed text** (a lossy projection optimized for repeated entities at scale). |
| Rebuild a broken view | `mempalace.repair.rebuild_index` / palace-daemon's `/repair mode=rebuild`. |
| Late-binding writes during view rebuild | palace-daemon's queue-and-drain (`<palace>/palace-daemon-pending.jsonl`). |
| Coordinate writes vs. view rebuild | palace-daemon's `_exclusive_palace()` semaphore acquisition during rebuild. |

The architecture _is_ this pattern. It's been built incrementally, in
response to specific failure modes (HNSW corruption, concurrent-writer
races, cold-start segfaults), without anyone having sat down to write
down the unifying frame. That's a healthy mode of evolution for a
system, but it leaves the frame implicit.

## What palace-daemon does, in frame terms

| Endpoint | Frame interpretation |
|---|---|
| `POST /silent-save` | Append to the log. During a view rebuild, queue the append; replay into the new view post-rebuild. |
| `GET /search` | Query the **semantic-search view**. |
| `GET /context` | Query the **semantic-search view**, formatted for LLM prompts. |
| `GET /stats` | Aggregate query across multiple views (KG, graph stats, status). |
| `POST /repair mode=light` | Invalidate cached view handles; next read re-opens. |
| `POST /repair mode=scan` | Read-only audit of view consistency. |
| `POST /repair mode=prune` | Remove corrupt rows from the view's underlying storage. |
| `POST /repair mode=rebuild` | Drop and recompute the view from log; coordinate writes via queue-and-drain. |
| `GET /repair/status` | Surface view-coordinator state to clients. |
| `POST /flush` | Force checkpoint of pending log entries. |
| `POST /reload` | Invalidate cached view handles (alias for `/repair mode=light` from a different vocabulary). |
| `POST /backup` | Snapshot the view storage (an eventual-fallback for view rebuilds, though log-based rebuild is the primary recovery path). |

Every endpoint slots into "log append," "view query," or "view
coordination." That's the coordinator role.

## What changes (and doesn't) under postgres backend (#665)

When mempalace's chroma backend is replaced by pgvector / pg_sorted_heap:

**Becomes simpler or disappears:**
- HNSW segment quarantine (`quarantine_stale_hnsw`) — no HNSW segment
  files; postgres manages indexes internally.
- Cold-start SIGSEGV warmup — pgvector doesn't have the chroma Rust
  binding's first-request crash class.
- The `_exclusive_palace()` rebuild lock — postgres handles MVCC and
  index rebuilds with `CREATE INDEX CONCURRENTLY`; daemon-mediated
  exclusion stops being necessary for vector index maintenance.
- The flock-based serialization in `ChromaCollection` — postgres
  transactions replace the file-level lock.

**Stays the same:**
- The view-coordinator role. `/silent-save`, `/search`, `/stats`,
  `/repair`, `/repair/status` — same shape, same semantics, same
  client contract.
- The queue-and-drain pattern — still useful as a backpressure mechanism
  during structural migrations, even if the underlying backend
  doesn't need exclusive access for normal index operations.
- Auth, routing, themed messages, structural-snapshot fast paths —
  all backend-agnostic.

**Becomes more interesting:**
- Multi-view coordination. With postgres as the backend, _all_ views
  (vectors, KG, structural) could live in one ACID-coordinated place.
  The daemon becomes less of a "ChromaDB-specific orchestration layer"
  and more of an "authoritative API surface for view queries."

## What this implies for palace-daemon's evolution

The strongest claim from this frame: **palace-daemon's value is its
role, not its implementation details.** The semaphores, queue-and-drain,
HNSW quarantine logic, exclusive-rebuild context manager — those are
implementations of the role, suited to ChromaDB's particular failure
modes. Replace ChromaDB with pgvector and most of those implementations
become unnecessary; the role itself remains.

This argues for keeping the daemon thin and focused on the role:
- HTTP/MCP API as the contract (stable across backends)
- Auth + routing
- View-rebuild coordination (only as needed by the backend)
- Optional structural fast paths for consumers (e.g. composing a single
  snapshot across wings + KG + tunnels in one HTTP roundtrip rather
  than forcing each adapter to fan out across multiple MCP calls).
  Under chroma the fast path can read the underlying sqlite directly;
  under postgres it'd be a direct-postgres aggregation query.

## What this implies for the knowledge-graph view

The KG is a small materialized view today (~6 entities, ~3 triples on
the canonical 151K-drawer palace). Under the Kleppmann frame, two
properties are interesting:

1. **It's a derived view.** Drop it, rebuild from the log. No data loss
   risk because the source isn't the KG sqlite — it's the conversations
   that produced the entity extractions.
2. **Different storage technologies serve different query patterns.**
   For 1-hop lookups against a few facts, plain SQL is right. If the
   KG ever grew to support multi-hop traversal at scale, Apache AGE
   (Cypher on postgres) would be the natural view technology.

The current scale doesn't justify AGE. The frame _does_ make it cheap
to experiment if/when that changes — drop the view, rebuild as AGE,
no permanent commitment.

## Open questions

These are honest open questions, not advocacy:

1. **Should the KG move to postgres alongside vectors?** The upstream
   #665 PR addresses pgvector but doesn't mention the KG. If both views
   live in one postgres database, ACID coordination across them
   becomes possible (a triple referencing a drawer ID can be enforced
   transactionally with the drawer's existence).

2. **Is the queue-and-drain pattern worth keeping under postgres?**
   Postgres handles concurrent writers via MVCC, but a write-during-
   schema-migration scenario could still benefit from a queue. The
   pattern is more useful than redundant.

3. **Should palace-daemon expose a "log replay" endpoint?** Today
   `/repair mode=rebuild` rebuilds from the existing chroma sqlite as
   the source. A future-proof variant would rebuild from the
   conversation transcripts — closer to the Kleppmann ideal, but
   requires holding onto the transcripts as first-class state.

4. **What's the right cadence for view rebuilds?** Today it's
   on-demand (`/repair mode=rebuild`). A more event-streaming-native
   approach would have continuous incremental view updates, but
   mempalace's current write rate doesn't justify that complexity.

## References

- Martin Kleppmann, _Designing Data-Intensive Applications_ (O'Reilly, 2017).
  The book that most clearly articulates the log-and-views pattern at
  general purpose.
- Kleppmann, [Turning the database inside-out with Apache Samza](https://www.confluent.io/blog/turning-the-database-inside-out-with-apache-samza/) — the talk version.
- [Apache AGE](https://age.apache.org/) — the candidate view technology
  for a richer KG view.
- [pgvector](https://github.com/pgvector/pgvector) — the candidate view
  technology for the semantic-search view post-postgres.
- [MemPalace#665](https://github.com/MemPalace/mempalace/pull/665) — the
  upstream postgres backend PR.

## Status

This document is a frame for ongoing thinking, not a roadmap. It exists
to make the implicit explicit so future architectural decisions
(backend swap, KG evolution, view experimentation) can be discussed in
a shared vocabulary. No code changes are tied to this document.
