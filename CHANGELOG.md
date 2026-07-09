## [Unreleased]

### Added
- **`/search` and `/context` accept optional `wing` and `room` filters** — both endpoints now forward `wing=` and `room=` query params into `mempalace_search` when supplied, mirroring the existing pass-through pattern on `/list`. When omitted the endpoints behave exactly as before, so existing clients are unaffected. Covers the common multi-tenant case where a caller wants to scope a semantic search to a single wing without filtering the result set client-side. Includes 7 unit tests (`tests/test_search_wing_filter.py`) covering back-compat + each filter combination.

# Changelog — continued

## [1.8.2] - 2026-07-06

### Fixed
- **systemd: restart-rate throttling silently disabled** — `StartLimitBurst`/`StartLimitIntervalSec` were placed in `[Service]` instead of `[Unit]`, which systemd ignores with a logged warning rather than an error. This let the daemon retry every `RestartSec` indefinitely instead of stopping after `StartLimitBurst` failures within `StartLimitIntervalSec`. Surfaced on a fresh deploy that crash-looped 400+ times in ~35 minutes while wired into `multi-user.target`, making the host appear to hang at boot.

## [1.8.1] - 2026-05-27

### Changed
- **mempalace upgraded to 3.3.6** — key fixes relevant to daemon operation: HNSW quarantine integrity gate (avoids false-quarantine when `dimensionality=None` but payload is sane), FTS5 validation at mine end, mine concurrency lock, MCP idle auto-exit (reduces stale ChromaDB handles). Note: `_hnsw_mtime_refresh_loop` workaround (v1.7.4) remains necessary — 3.3.6's integrity gate still returns unhealthy for segments with no `index_metadata.pickle` and >1KB `data_level0.bin`, which is all ChromaDB 1.5.x segments.
- **Repair progress tracking refactored** — replaced the stdout-redirect hack (`_RebuildProgressBuffer` + `sys.stdout = buf`) with the proper `progress=` callback added to `rebuild_index` in mempalace 3.3.6 (jphein's PR #1487). Uses `functools.partial` to thread the callback into the executor call. Cleaner, forward-compatible, and won't silently break if upstream changes its print format. Removed `import contextlib`, `import io` (now unused).

## [1.8.0] - 2026-05-27

### Added
- **Verified backups** — `/backup` now performs three-step verification before reporting success: (1) `sqlite3.backup()` snapshot, (2) `PRAGMA integrity_check` on the backup file, (3) smoke retrieval — opens backup read-only and queries `embedding_metadata LIMIT 5`. Returns a structured response: `{backup_path, timestamp, integrity, smoke_test, rows_sampled, status}` where `status` is `"ok"` or `"degraded"`. Backup file is always kept on failure for forensics. All sync I/O runs via `loop.run_in_executor`. Empty palaces (no `embedding_metadata` table) return `smoke_test: "WARN: ..."` rather than a failure.
- **Crash-loop configurable thresholds** — `_CRASH_LOOP_WINDOW`, `_CRASH_LOOP_THRESHOLD`, and a new `_CRASH_LOOP_RECOVERY` constant are now readable from env vars `PALACE_CRASH_LOOP_THRESHOLD_SECONDS` (default 600), `PALACE_CRASH_LOOP_THRESHOLD_COUNT` (default 3), and `PALACE_CRASH_LOOP_RECOVERY_SECONDS` (default 1800).
- **Crash-loop auto-recovery** — if the daemon has been running longer than `_CRASH_LOOP_RECOVERY` seconds, `_crash_loop_state()` returns `crash_loop: False, recovered: True`, and `/health` returns 200 again without manual intervention. `uptime_seconds` is now included in the state dict.
- **Crash-loop desktop notification** — on crash-loop detection at startup, fires `notify-send --urgency=critical` so the operator sees it without watching logs. Silently catches `FileNotFoundError` for headless/container environments. Closes #21.
- **Repair rebuild progress + ETA in `/repair/status`** — during a `mode=rebuild` repair, `/repair/status` now includes a `progress` key with `staged_current`, `staged_total`, `refiled_current`, `refiled_total`, and `eta_seconds` (ETA derived from per-drawer throughput). Implemented via `_RebuildProgressBuffer` (a `TextIOBase` subclass that captures `rebuild_index` stdout) and `_capture_rebuild_progress` context manager. `_internal` keys (e.g. `_start_mono`) are filtered from the API response.

## [1.7.5] - 2026-05-27

### Fixed
- **Security: constant-time API key comparison** — replaced `!=` with `hmac.compare_digest` in `_check_auth()` to prevent timing side-channel attacks. Also handles `x_api_key=None` gracefully (no 500 on missing header when auth is enabled).
- **Hook auth: X-API-Key missing from daemon HTTP calls** — added `_auth_headers()` helper to `clients/hook.py` that reads `PALACE_API_KEY` from env and injects the `X-Api-Key` header into all three hook POST functions (`_post_digest`, `_post_mcp`, `_post_mine`). Without this, hooks silently received 401 from an auth-enabled daemon.
- **Repair deadlock: client cache cleared before rebuild** — `_mp._client_cache` and `_mp._collection_cache` are now dropped *before* `rebuild_index` runs, not after. The previous order left the cached `PersistentClient` holding the SQLite lock during the rebuild, causing a deadlock under ChromaDB 1.x.
- **Hook exchange counter: tool_result messages miscounted** — `_count_human_messages` was incrementing for human-turn entries whose entire content was `tool_result` blocks (no text). These are tool roundtrips, not human exchanges. Fixed by requiring at least one `text`-type block before counting. Also self-heals a backward counter: if `last_save > exchange_count` (can happen after a count-rule change), `last_save` is rebased to `exchange_count` with a log entry so `since_last` never goes negative.
- **Hook Stop/PreCompact killed by harness pipe closure** — added `_detach()` to `clients/hook.py`: forks a background child that becomes a session leader (`setsid`) and redirects all three stdio FDs to `/dev/null`. The parent prints the harness response and exits immediately; the child does the slow diary save or mine without risking SIGPIPE when the harness closes its end. Falls back to synchronous execution if `fork()` fails.
- **`POST /mine` concurrent ChromaDB client corrupts log store** — `/mine` spawned `mempalace mine` as a subprocess that opened its own `PersistentClient` on the palace path while the daemon already held one. Two `PersistentClient` instances on one chroma path corrupt the Rust log store (in- or cross-process), causing drawers to silently persist nowhere. Fix: on chroma, the mine is now wrapped in an `_exclusive_palace()` lock + `_drop_chroma_client(close=True)` + subprocess + reopen in `finally`, making the subprocess the sole client for the duration. On postgres, concurrent connections are native so the original `_mine_sem` path is kept. Includes 4 unit tests (`tests/test_mine_backend_aware.py`). `PALACE_CHROMA_FLUSH_SECONDS` (default `0.0`) env var available as a post-close settle margin.

## [1.7.4] - 2026-05-26

### Fixed
- **False-positive HNSW quarantine with ChromaDB 1.5.x** — `quarantine_stale_hnsw` (mempalace) marks a segment unhealthy when `index_metadata.pickle` is absent and `data_level0.bin` exceeds 1 KB. ChromaDB 1.5.x (Rust HNSW backend) never writes `index_metadata.pickle`, so every non-trivial segment failed this check regardless of health. Consequence: every `mempalace status` call or daemon restart after ≥300 s of uptime quarantined the live segment and forced a full HNSW rebuild from SQLite. Fix (daemon-side workaround): added `_hnsw_mtime_refresh_loop` — a background task that `touch`es `data_level0.bin` in every live segment every 60 s, keeping the sqlite/HNSW mtime gap below the 300 s threshold so the quarantine path is never reached. Skips during active repairs.

## [1.7.3] - 2026-05-23

### Fixed
- **Watchdog kills rebuild** — `_watchdog_loop` was `continue`-ing past `_sd_notify("WATCHDOG=1\n")` during `mode=rebuild`, so systemd received no keepalive and killed the process at the 2-minute watchdog timeout (SIGABRT). Fix: send `WATCHDOG=1` unconditionally before `continue`-ing — the collection probe is still skipped (to prevent the WAL race from v1.7.1), but systemd gets its ping and the rebuild runs to completion.

## [1.7.2] - 2026-05-23

### Fixed
- **`_warn_if_hnsw_threads_unset` false positive** — function was calling `handle_request(ping)` to warm up the collection before reading `_collection_cache`. MCP ping is a protocol-level echo that never touches the ChromaDB collection, so after any cache clear (light/prune/rebuild repair, auto-repair quarantine) `_collection_cache` remained `None`, causing `hnsw:num_threads` to read as `None` and the warning to fire spuriously. Fixed by calling `_mp._get_collection()` instead, which actually opens the collection (applying `_pin_hnsw_threads` from mempalace 3.3.4) and populates the cache before the metadata check. Also removes the stale "Upgrade to mempalace >=3.3.4" hint (3.3.4 is the installed version and already has the fix). Side effect: after any cache-clearing repair, the collection is proactively re-pinned before the first incoming request rather than on it.

# Changelog — continued

## [1.7.1] - 2026-05-23

### Added
- **Crash-loop detection** — daemon tracks restart timestamps in a ring buffer (`~/.cache/palace-daemon/restart_history.json`). If ≥3 restarts occur within 10 minutes, `/health` returns 503 with `status: "crash_loop"` and `restart_count` / `window_seconds` fields, making persistent failures visible to monitoring. `palace-daemon.service` now adds `StartLimitBurst=5` / `StartLimitIntervalSec=600` so systemd stops retrying after 5 crashes in 10 minutes.

### Fixed
- **`GET /stats` concurrent HNSW race** — serialized the three parallel `mempalace_kg_stats` / `mempalace_graph_stats` / `mempalace_status` calls; concurrent execution raced on the ChromaDB HNSW index (issues #974/#965) causing SIGBUS on palaces with stale or rebuilding segments.
- **Watchdog/rebuild race** — systemd watchdog loop now skips the `_get_collection()` probe during `mode=rebuild` repairs. Previously the watchdog fired concurrently mid-collection-swap, opening a SQLite read handle while the rebuild was rewriting WAL files, causing `SQLITE_IOERR_READ` (code 522) and database corruption on the next open.

# Changelog

## [1.7.0] - 2026-05-23

### Added
- **`GET /viz`** — self-contained status dashboard. Single HTML page that fetches `/graph`, `/repair/status`, and `/health` in parallel and renders five panels: status strip (version, drawer count, repair pulse, pending writes), D3 force-directed knowledge graph, wing/room hierarchy (Mermaid tree), wings bar chart, tunnels list with click-to-highlight. D3 + Mermaid loaded via CDN (pinned versions, SRI-verified), no static-file deps. Optional `?refresh=N` for auto-refresh, `?key=…` for ergonomic auth bookmarking (key leaks into browser history — prefer `X-Api-Key` header beyond personal bookmarks). HTML template at `static/viz.html`; lazy-loaded and cached in-process. Auth-gated; all user-visible strings enter DOM via `textContent`/safe `setAttribute`, Mermaid runs `securityLevel: "strict"`, CDN assets SRI-pinned (`d3@7.8.5`, `mermaid@10.9.1`).
- **`GET /graph`** — single-shot structural snapshot for SME-style consumers. Composites wings, rooms, tunnels, KG stats in parallel via `asyncio.gather`; reads `chroma.sqlite3` and `knowledge_graph.sqlite3` directly in read-only mode. Cuts multi-call latency from 60–120 s to ~0.4 s on a 151K-drawer palace. Degrades gracefully on schema drift. Spec: `docs/graph-endpoint.md`.
- **`GET /list`** — query-free metadata browse. Wraps `mempalace_list_drawers`; filter by `?wing=` and/or `?room=`.
- **`DELETE /memory/{id}` + `PATCH /memory/{id}`** — drawer-level CRUD endpoints for curator UIs. Delete wraps `mempalace_kg_invalidate`-style removal; patch wraps `mempalace_update_drawer`.
- **`feat(lifespan): auto-migrate Stop-hook checkpoints`** — daemon startup calls `mempalace.migrate.migrate_checkpoints_to_recovery()` (idempotent). Gated by `PALACE_MIGRATE_CHECKPOINTS=1` env var; no-op when unset.
- **`feat: canonicalize Stop-hook topic`** — daemon rewrites legacy `"auto-save"` synonyms to canonical `"checkpoint"` topic at the `/silent-save` boundary, with a warning log entry. Defensive; safe for already-shipped clients.
- **`scripts/verify-routes.sh`** — curl-based smoke-test script covering all read-only daemon routes. Run post-deploy to confirm no regressions.
- **`clients/mempal-fast.py`** — `CHECKPOINT_TOPIC` constant added (mirrors `hook.py`); eliminates hardcoded string duplication.

### Fixed
- **`/search` and `/context` `limit=` parameter** — param name mismatch caused queries to always cap at 5 results regardless of the `limit=` argument. Fixed.
- **`clients/palace-mcp-dispatch.sh` portable path** — hardcoded absolute path replaced with `readlink`-based sibling lookup; works on any machine.
- **`clients/palace-mode` embedded secrets removed** — hardcoded homelab URL and API key replaced with env-var-only config with fail-fast guards.

### Docs
- **`docs/event-log-frame.md`** — architectural reference framing palace-daemon under Kleppmann's log + materialized-views pattern; context for future Postgres-backend and TypeScript-rewrite discussions.

### Maintenance
- Upgraded mempalace to 3.3.5. `mcp_server_get_collection.patch` removed — retry-on-failure with cache clearing and error logging landed upstream (#1377, #1396). `apply_patches.sh` now exits clean with no patches to apply.

# Changelog

## [1.6.0] - 2026-05-11

### Added
- **`POST /digest`** — async AAAK summarisation endpoint. Accepts a transcript excerpt
  (`session_id`, `agent_name`, `harness`, `messages`, `exchange_count`), fires a background
  task that calls the Anthropic API (`claude-haiku-4-5`) and writes the result to the diary
  via `tool_diary_write`. Returns 202 immediately. Requires `ANTHROPIC_API_KEY`; returns 503 gracefully when absent.
- **`clients/backfill.py`** — one-shot script to retroactively index existing Claude Code
  JSONL transcripts into the MemPalace diary. Reads all sessions under `~/.claude/projects/`,
  extracts user turns, formats as AAAK, writes diary entries via the daemon.
  Supports `--dry-run`, `--min-turns`, `--projects-dir`, `--harness`.

### Changed
- **`clients/hook.py` silent saves now write real AAAK content** — the `AUTO-SAVE:session_id|N.msgs|...`
  stub is replaced by a `SESSION:date|harness+Nmsgs|★★★☆☆` entry containing the last 10 user
  turns extracted from the session JSONL. No API key required; falls back to stub if the
  transcript is unreadable or the daemon is unreachable.

### Fixed
- **`mcp_server_get_collection.patch` updated for mempalace 3.3.4** — upstream refactored
  `get_or_create_collection` into a split get/create to avoid a Rust-binding SIGSEGV (#1262).
  Patch rebased onto new code; retry logic, cache clearing, and error logging preserved.
  `apply_patches.sh --check` now reports `[already applied]` on 3.3.4.

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
