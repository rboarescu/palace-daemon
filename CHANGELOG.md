# Changelog

## [Unreleased]

### Added
- **`GET /viz`** — self-contained status dashboard. Single HTML page that fetches `/graph`, `/repair/status`, and `/health` in parallel and renders five panels: status strip (version, drawer count, repair pulse, pending writes), D3 force-directed knowledge graph, wing/room hierarchy (Mermaid tree), wings bar chart, tunnels list with click-to-highlight. D3 + Mermaid loaded via CDN, no static-file deps. Optional `?refresh=N` for auto-refresh, `?key=…` for ergonomic auth bookmarking.
- Inspired by upstream MemPalace PRs #1022 (sangeethkc — D3 KG viz), #393 (jravas — Mermaid diagrams), #431 (MiloszPodsiadly — CLI stats), #256 (rusel95 — sync_status MCP), #601 (mvanhorn — brief overview). None cherry-picked; the page consumes the daemon's own `/graph` endpoint so it benefits from the direct-sqlite optimization (sub-second on 151K drawers) and stays decoupled from upstream's evolution.
- Security: all wing/room/entity names from `/graph` enter the DOM via `textContent` / safe `setAttribute`, never `innerHTML`. Mermaid labels pass through a sanitizer that strips `[`, `]`, `"`, `<`, `>`, `|`, `` ` `` to avoid breaking the parser. CDN-loaded D3 + Mermaid are the only third-party scripts.
- HTML template at `static/viz.html`; cached at module load. New endpoint defined alongside `/graph` in `main.py`.

### Added
- **`GET /graph`** — single-shot structural snapshot for SME-style consumers. Mirrors `/stats`'s `asyncio.gather` shape but adds rooms-per-wing fan-out + a direct read-only sqlite read of `knowledge_graph.sqlite3`. Replaces what an adapter would otherwise compose serially over HTTP — on a 151K-drawer palace, `list_wings` alone takes ~30s, so a serial composition costs minutes.
- Response shape: `{ "wings": {<name>: <count>, ...}, "rooms": [{"wing": "<name>", "rooms": {<room>: <count>, ...}}, ...], "tunnels": [...], "kg_entities": [...], "kg_triples": [...], "kg_stats": {...} }`.
- KG read uses URI-mode `?mode=ro` so the daemon can never accidentally write that file. Schema differences across mempalace versions tolerated via per-query `OperationalError` catch.
- Wings + rooms read directly from `chroma.sqlite3.embedding_metadata` (read-only, off the asyncio loop), bypassing the `list_wings` + `list_rooms × N` MCP fan-out which serializes through the read semaphore. Schema is ChromaDB's internal layout; if it ever drifts, /graph degrades gracefully to empty wings/rooms.
- Tunnels derived from `mempalace_graph_stats.top_tunnels` rather than `mempalace_list_tunnels` — the two disagree on what counts as a tunnel on mempalace 3.3.4 (`list_tunnels` returns `[]`, `graph_stats.tunnel_rooms` reports the real count). Spec at `docs/graph-endpoint.md` Part 2 for the upstream-mempalace fix.
- Spec / design notes: `docs/graph-endpoint.md`. Coordinates with `multipass-structural-memory-eval` (SME) — adapter prefers `/graph` once daemon ≥ this release and falls back to MCP composition otherwise.

### Maintenance
- `mcp_server_get_collection.patch` — synced hunk offsets to mempalace 3.3.3; removed redundant `collection.modify()` re-application block since upstream now handles `hnsw:num_threads=1` enforcement via `_pin_hnsw_threads()`. Patch behaviour is unchanged.

## [1.5.1] - 2026-04-26

### Fixed
- **`_get_collection` silent failures** -- exceptions are now logged (palace path + error) instead of silently returning `None`. Cache-staleness incidents are now visible in the daemon log.
- **Stale collection cache self-healing** -- `_get_collection` retries once after clearing all caches (`_client_cache`, `_collection_cache`, `_metadata_cache`) on failure. The incident that required a manual daemon restart now self-heals on the next tool call.
- **HNSW `num_threads=1` enforced on every open** -- `_get_collection` calls `collection.modify()` after every open, merging `hnsw:num_threads=1` into existing metadata. ChromaDB 1.5.x does not persist HNSW metadata across reopens (issue #1161); without this, every cache clear silently re-enabled parallel inserts and risked SIGSEGV under concurrent writes.
- **`/health` reflects actual palace state** -- previously returned HTTP 200 `ok` even when the collection was broken (false healthy). Now calls `_get_collection()` and returns HTTP 503 `degraded` if the palace is unavailable.

### Added
- **Systemd watchdog** -- daemon sends `READY=1` on startup and `WATCHDOG=1` every `WatchdogSec/2` seconds via `sd_notify` (stdlib-only, no external deps). Watchdog pings are gated on a live `_get_collection()` check: if the palace goes dark, the watchdog goes silent and systemd kills and restarts the daemon.
- `palace-daemon.service` updated: `Type=simple` changed to `Type=notify`, `NotifyAccess=main` added, `WatchdogSec=120` added. Re-install: `sudo cp palace-daemon.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart palace-daemon`.
- **Startup warmup opens the collection** -- lifespan warmup now calls `_get_collection(create=True)` directly instead of `ping`. `ping` never touches the collection, so `num_threads=1` was not applied before `_warn_if_hnsw_threads_unset` ran at startup, causing a spurious warning on every boot. The warning is now silent on a healthy palace.

## [1.5.0] - 2026-04-24

### Added
- **`POST /repair`** — coordinates repairs with daemon-mediated traffic. Four modes:
  - `light` — clears client/collection caches; next open re-runs `quarantine_stale_hnsw()`. Cheap, non-blocking for other callers.
  - `scan` — runs `mempalace.repair.scan_palace` under a read slot, returns the count of corrupt IDs found.
  - `prune` — runs `mempalace.repair.prune_corrupt` under a write slot; the cross-process flock in `ChromaCollection` already serializes this against live writers.
  - `rebuild` — destructive collection swap (`delete_collection` + `create_collection` are *outside* the flock, so a naked rebuild concurrent with any writer silently loses writes). Holds every read/write/mine semaphore slot during the rebuild window to prevent daemon-mediated writes from racing the swap.
- **`POST /silent-save`** — HTTP path for Claude Code Stop-hook silent saves. Normal ops: writes a diary checkpoint via `tool_diary_write` under the write semaphore. During `/repair mode=rebuild`: appends the payload to `<palace_parent>/palace-daemon-pending.jsonl` and returns a themed "held in trust" message. The queue drains automatically once the rebuild completes.
- **`GET /repair/status`** — current repair state + pending-writes queue depth.
- **Themed messages** — `messages.py` centralizes user-facing strings for save, save-queued, repair-begin, repair-complete, and drain-fail paths. Saves use `✦`; palace ops use `◈`.

### Notes
- The rebuild coordination is daemon-scoped: external `mempalace repair rebuild` CLI invocations still race against any other process's writes because `delete_collection` / `create_collection` are backend-level operations that the `ChromaCollection` flock does not protect. For safe concurrent rebuilds, route through the daemon.
- Fork's `mempalace/hooks_cli.py` opt-in: set `PALACE_DAEMON_URL` (and optionally `PALACE_API_KEY`) and silent Stop-hook saves will POST to the daemon, picking up the queue-and-drain behavior and themed messages. Unset or unreachable → falls through to the legacy direct-write path with no behavior change.

## [1.4.5] - 2026-04-25

### Changed
- **`clients/hook.py` — time-based and session-end saves**
  - `TIME_SAVE_INTERVAL` (300 s) was defined but never used; now wired in as a second independent save trigger in `hook_stop`. Saves fire if ≥5 min have elapsed with any unsaved exchanges, regardless of the 15-exchange count gate.
  - New `force_on_stop` setting (default `true`) adds a third trigger: saves whenever `since_last > 0` and at least `force_min_interval` seconds (default 60 s) have passed since the last save. Captures session-end stops that fall below the exchange-count threshold.
  - `force_min_interval` is now configurable via `hook_settings.json` (falls back to hardcoded `FORCE_MIN_INTERVAL = 60`).
  - `hook_session_start` seeds `{session_id}_last_save_ts` at session open so the first Stop of a new session doesn't spuriously fire the time trigger.
  - `hook_session_start` now prunes state files older than 7 days from `~/.mempalace/hook_state/` to prevent unbounded accumulation.
  - Module docstring updated to list all four `hook_settings.json` keys (`force_on_stop`, `force_min_interval` added).
  - Diary auto-save entries now embed the trigger reason (`hook.count`, `hook.time`, `hook.force`).

## [1.4.2] - 2026-04-24

### Fixed
- **Backup connection leak** — `POST /backup` now wraps both SQLite connections (`src`/`dst` and `check`) in `try/finally` blocks so they are always closed even when backup or integrity check fails.
- **World-writable lock file** — daemon lock file moved from `/tmp/palace-daemon-{port}.lock` to `~/.cache/palace-daemon/daemon-{port}.lock` (directory created with mode `0o700`).
- **`/mine` path traversal** — `POST /mine` now validates that `dir` is an absolute path with no `..` components, exists, and is a directory. Rejects invalid input with 400 before spawning a subprocess.
- **HNSW retry on write ops** — auto-repair retry in `_call()` is now restricted to `_READ_TOOLS`; write ops get a diagnostic hint instead of a retry that could produce duplicate drawers.
- **`bootstrap.sh` silent scp failure** — each `scp` call now has an explicit `|| { echo ...; exit 1; }` guard with a descriptive error message.

### Changed
- **`POST /backup` dir permissions** — backup directory created with `mode=0o700` instead of default umask.
- **`/mine` param validation** — `mode` validated against `{convos, projects}`, `extract` against `{exchange, general}`, `limit` coerced to `int` with clear 400 errors.
- **`bootstrap.sh` env overrides** — `ARTEMIS_HOST` and `ARTEMIS_CLIENTS_PATH` are now overridable via environment variables (defaults unchanged).
- **Debug scripts** — `rebuild_v3.py`, `refresh_index.py`, `repair_rebuild_surgical.py`, `stress_test.py`, `purge_wings.py` moved to `scripts/`; `main.py.bak` deleted.
- **`palace-daemon.service`** — `ExecStartPre` stale-lock path updated to match new lock location (`~/.cache/palace-daemon/daemon-8085.lock`).

## [1.4.1] - 2026-04-24

### Added
- **`purge_wings.py`** — offline SQLite-based wing purge utility; bypasses ChromaDB entirely to safely bulk-delete drawers when HNSW is stale or corrupt. Deletes in 500-item batches, backs up before changes, clears HNSW segment dirs so daemon rebuilds a clean index on next start.

### Changed
- `palace-daemon.service` hardened with two `ExecStartPre` guards: `fuser -k 8085/tcp` clears any stale process holding the port; `rm -f /tmp/palace-daemon-8085.lock` removes a stale lock file. Both prefixed with `-` so they're no-ops when nothing is blocking.
- README systemd section updated: system service is now the recommended install for always-on hosts; user service demoted to desktops/dev only. Added `WARNING` callout against installing both (causes crash-loop collision on port 8085).
- Bumped `VERSION` to `1.4.1`.

### Fixed
- Removed 10,828 rogue drawers mined from `~/.` and `~/palace-daemon/` by the old `mempalace hook run` fallback. Palace reduced from 11,632 → 685 real drawers. Vector index rebuilt via `mempalace repair`.
- Resolved dual user+system service collision that caused crash-loop on `systemctl restart palace-daemon`.

## [1.4.0] - 2026-04-24

### Added
- **`clients/hook.py`** — stdlib-only hook runner replacing `mempalace hook run`. Routes all mine operations through `POST /mine` on palace-daemon; never spawns mempalace as a subprocess. If daemon is unreachable, passes through silently with no fallback to direct DB access.
- **`clients/bootstrap.sh`** — one-command client setup script. Copies `mempalace-mcp.py` and `hook.py` from Artemis and wires them into Claude Code, Gemini CLI, VSCode, Cursor, or JetBrains. Clients need no mempalace install — both files are stdlib-only.
- **`docs/hook-routing-fix.md`** — permanent plan document capturing the hook routing fix design, constraints, and verification steps.

### Changed
- `~/.claude/settings.json` and `~/.gemini/settings.json` hook commands updated to use `hook.py` instead of `mempalace hook run`.
- `~/.mempalace/hook_settings.json` `daemon_url` normalised to `http://localhost:8085` (was `10.0.0.5:8085`) on Artemis.
- `README.md` — expanded Clients section: remote client table, `hook.py` usage and behaviour, `hook_settings.json` field reference, per-tool hook configs, `bootstrap.sh` usage.

### Security
- Mine operations now require explicit user approval via block response before executing; no implicit auto-mine on session stop.
- `MEMPAL_DIR` is the only mine trigger; transcript directory fallback removed.

## [1.3.0] - 2026-04-24

### Added
- **Auto-Healing HNSW Index** — daemon now automatically detects "Internal error: Error finding id", quarantines stale index segments via `quarantine_stale_hnsw`, and retries the request seamlessly.
- **Silent Save / Flush** — implemented automatic memory checkpointing on daemon shutdown via `lifespan` and added a manual `POST /flush` endpoint.
- **Port-Specific Locking** — lock files are now dynamic (`/tmp/palace-daemon-{port}.lock`), allowing parallel instances (e.g., production and shadow/test palace) on the same host.
- **Chaos Test Suite** — added `chaos_test.py` for high-concurrency validation and index corruption simulation.

### Changed
- Updated `README.md` with "Shadow Palace" testing workflow and new API endpoints.
- Improved logging for auto-repair events to provide better visibility during recovery.
- Bumped internal version to 1.3.0.

## [1.2.0] - 2026-04-23

### Added
- **Daemon Instance Locking** — implemented fcntl-based file lock (`/tmp/palace-daemon.lock`) to prevent multiple daemon instances from running concurrently
- **Graceful Shutdown** — added SIGINT/SIGTERM handlers to ensure clean exits and reduce risk of stale SQLite locks during restarts
- **Hardened Service** — `palace-daemon.service` now enforces explicit environment paths (`MEMPALACE_PALACE`) to prevent path ambiguity

### Changed
- **"Daemon-Only" Policy** — removed the direct fallback mode in `mempalace-mcp.py`. The client now exits with an error if the daemon is unreachable. This prevents "split-brain" scenarios and potential database corruption from concurrent process access.
- Improved HNSW stale index detection to catch more internal error variations and provide specific recovery commands.

### Security & Stability
- Added high-visibility warnings against accessing the database over network mounts (NFS/Samba) which caused `SQLITE_IOERR` in previous versions.

## [1.1.2] - 2026-04-23

### Added
- `POST /backup` endpoint — performs atomic, verified SQLite backups with integrity checks
- `POST /reload` endpoint — clears internal client cache to refresh the database index
- Self-healing hints — the daemon now detects "Internal error: Error finding id" during searches and provides actionable advice

### Fixed
- `palace-daemon.service` — port conflict handling: added `ExecStartPre=-/usr/bin/fuser -k 8085/tcp` to ensure port 8085 is free before starting
- Improved service reliability by adding `KillMode=mixed` to `palace-daemon.service`
- `main.py` — added `VERSION` constant and exposed it in `/health`

### Changed
- Updated documentation with API references for new endpoints and clearer systemd instructions

## [1.1.1] - 2026-04-22

### Fixed
- `clients/mempalace-mcp.py` — SyntaxError on startup: `--api-key` argument
  used `default=API_KEY` before `global API_KEY` declaration; changed default
  to `None` so the client actually starts

## [1.1.0] - 2026-04-22

### Added
- `PALACE_MAX_CONCURRENCY` env var (default 4) — tunes read concurrency at runtime
- `clients/mempalace-mcp.py` fallback mode — if the daemon is unreachable at
  startup, falls back to importing `mempalace.mcp_server` in-process instead
  of exiting, so Claude Code keeps working when the daemon is down

### Changed
- Replaced `asyncio.Lock()` with three semaphores for concurrent access control:
  - `_read_sem(N)` — up to N concurrent read-only ops (search, query, stats, …)
  - `_write_sem(N//2)` — up to N//2 concurrent write ops (add, kg mutations, …)
  - `_mine_sem(1)` — one mine job at a time, independent of reads/writes
- `/mine` now uses `_mine_sem` only — long import jobs no longer block read or
  write traffic (requires mempalace ≥3.3.2 for internal mine locking)
- `/health` bypasses all semaphores — always responds immediately even under
  full load, safe for load balancers and monitoring
- `/stats` fans out its three sub-calls with `asyncio.gather()` — response time
  cut to roughly one third of the previous sequential implementation

## [1.0.0] - 2026-04-21

### Added
- `POST /mcp` — full MCP JSON-RPC proxy endpoint
- `GET /health` — daemon + palace status
- `GET /search` — semantic search over palace drawers
- `GET /context` — alias for /search, named for LLM tool prompts
- `POST /memory` — store a drawer (wing, room, content)
- `GET /stats` — wing/room counts, KG stats
- `POST /mine` — run `mempalace mine` under the global asyncio.Lock,
  serializing bulk imports against live queries
- Optional API key auth via `PALACE_API_KEY` env var (`X-Api-Key` header)
- Configurable host, port, palace path via CLI args or env vars
- `clients/mempalace-mcp.py` — zero-dependency stdio MCP proxy for remote clients
- systemd service unit (`palace-daemon.service`)
