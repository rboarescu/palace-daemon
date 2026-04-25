# palace-daemon

An HTTP/MCP gateway for [MemPalace](https://github.com/MemPalace/mempalace) that coordinates concurrent access to the palace through a single process.

## Why

When multiple clients hit MemPalace simultaneously — an AI agent, an Android app, a bulk import job — you want a single chokepoint that controls throughput and keeps mining jobs from starving live queries. palace-daemon provides this through three asyncio semaphores: a read semaphore (N concurrent), a write semaphore (N/2 concurrent), and a mine semaphore (1 at a time). MemPalace ≥3.3.2 handles correctness internally (WAL mode, KG instance lock, mine PID guard); the daemon handles coordination.

## Stability & Concurrency

> [!CAUTION]
> **CRITICAL: NEVER mount the palace database via NFS/Samba for direct access.**
> SQLite and ChromaDB are not network-safe. Direct access over a network mount will cause `SQLITE_IOERR`, HNSW index corruption, and permanent data loss. Always use `palace-daemon` over HTTP for remote access.

### The "Daemon-Only" Policy
To prevent database corruption, this project enforces a strict **Single-Process Access** model:
1.  **Daemon Lock:** `main.py` uses a file lock (`/tmp/palace-daemon-{port}.lock`) to prevent multiple daemon instances from fighting over the database.
2.  **Systemd-First:** Manual startup is blocked by default to prevent "split-brain" scenarios between a system service and an agent's manual process.
3.  **No Client Fallback:** The `mempalace-mcp.py` client is hard-coded to **fail** if it cannot reach the daemon. It will no longer attempt to open the database files directly.

## Features

- **Self-Healing Startup** — `--force` flag automatically clears stale processes on the target port
- **Collection cache auto-retry** -- if the internal ChromaDB collection cache goes stale, `_get_collection` clears all caches and retries once automatically before returning an error
- **HNSW thread safety** -- `num_threads=1` is enforced on every collection open, not just creation; prevents SIGSEGV from parallel inserts after any cache clear (ChromaDB 1.5.x issue #1161)
- **Systemd watchdog** -- sends `READY=1` on startup and `WATCHDOG=1` every 60s (gated on a live collection check); systemd restarts the daemon if the palace goes dark
- **Protected Manual Start** — requires `--manual` flag for debugging, preventing accidental agent starts
- **MCP proxy** — any MCP client connects to /mcp instead of spawning a local process
- **REST API** — search, store, and query the palace over HTTP (Android app, netdash, scripts)
- **Concurrent access control** — three semaphores coordinate reads, writes, and mine jobs; tunable via `PALACE_MAX_READ_CONCURRENCY` / `PALACE_MAX_WRITE_CONCURRENCY`
- **Isolated mining** — /mine runs under its own semaphore so bulk imports never stall live traffic
- **Optional API key auth** — set `PALACE_API_KEY` to protect all write endpoints

## Requirements

- Python 3.12+
- mempalace installed (pipx recommended)

    pip install -r requirements.txt

## Usage

    # Recommended: Use systemctl (see systemd section)
    sudo systemctl start palace-daemon

    # Manual start (Debugging only)
    python main.py --manual --palace ~/.mempalace/palace --port 8085

    # Force start (Clears port 8085 first)
    python main.py --manual --force

    # With API key auth
    PALACE_API_KEY=your-secret python main.py --manual


## Security

> **Do not expose port 8085 to the internet without setting `PALACE_API_KEY`.**
> The `/mine` endpoint accepts arbitrary filesystem paths — anyone with access
> can trigger reads from any directory on your server.

For local network use, leaving auth disabled is fine. For remote access, always set an API key:

    PALACE_API_KEY=your-secret python main.py

## systemd

### System service (Recommended for servers)

Starts at boot, before any user session. Use this on Artemis or any always-on host.

    sudo cp palace-daemon.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now palace-daemon

### User service (desktops / dev machines only)

Only runs while you're logged in. Use this if you don't have sudo or only need the daemon during your session.

    mkdir -p ~/.config/systemd/user/
    cp palace-daemon.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now palace-daemon

> [!WARNING]
> **Never install both.** Running a system service and a user service simultaneously causes a port 8085 collision — the second instance will crash-loop with "Another instance already running". Pick one and remove the other.
>
> To remove a previously installed user service:
>
>     systemctl --user stop palace-daemon
>     systemctl --user disable palace-daemon
>     rm ~/.config/systemd/user/palace-daemon.service
>     systemctl --user daemon-reload

Edit `palace-daemon.service` to set `PALACE_API_KEY` or a custom `--palace` path before installing.

The service uses `Type=notify` and `WatchdogSec=120`: the daemon signals systemd when it is ready and sends a watchdog heartbeat every 60 s. If the watchdog goes silent (e.g. the palace collection breaks), systemd kills and restarts the daemon automatically.

## Troubleshooting

### Palace reports `degraded` on `/health`
The daemon is running but cannot open the ChromaDB collection. Since 1.5.1, `_get_collection` will attempt a self-heal automatically on the next tool call. If it persists:

    curl -X POST http://localhost:8085/reload    # clear client cache
    sudo systemctl restart palace-daemon         # full restart if reload fails

Check `journalctl -u palace-daemon -n 50` for the logged exception — it will now show the exact error instead of a silent `None`.

### Port 8085 already in use
If the daemon fails to start with `[Errno 98] address already in use`, it usually means a previous instance didn't shut down cleanly.

`palace-daemon.service` includes an `ExecStart` command that uses `--force` to clear the port automatically. If running manually, use the `--force` flag:

    python main.py --manual --force

To manually clear the lock and port without starting:

    fuser -k 8085/tcp && rm -f ~/.cache/palace-daemon/daemon-8085.lock


## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /health | Daemon + palace status; returns HTTP 503 `degraded` if collection is unavailable |
| POST | /backup | Atomic verified SQLite backup |
| POST | /reload | Clear client cache / refresh index |
| POST | /repair | Coordinate repair with daemon traffic (`mode`: `light`/`scan`/`prune`/`rebuild`) |
| GET | /repair/status | Current repair state + pending-writes queue depth |
| POST | /silent-save | Stop-hook silent save; queues during `/repair mode=rebuild` |
| GET | /stats | Wing/room counts, KG stats |
| GET | /search?q=...&limit=5 | Semantic search |
| GET | /context?topic=... | Same as search, named for LLM use |
| POST | /memory | Store a drawer {content, wing, room} |
| POST | /mcp | Full MCP JSON-RPC proxy |
| POST | /mine | Bulk import under lock |

### /mine — bulk import

    curl -X POST http://localhost:8085/mine \
      -H 'Content-Type: application/json' \
      -d '{"dir": "/path/to/files", "wing": "gemini", "mode": "convos"}'

Body: dir (required), wing, mode (projects/convos), extract (exchange/general), limit.

Mine jobs run one at a time under their own semaphore. Read and write traffic continues unblocked during a mine job.

### /repair — coordinate a repair

    curl -X POST http://localhost:8085/repair \
      -H 'Content-Type: application/json' \
      -d '{"mode": "rebuild"}'

Modes:

- **`light`** — clear cached client/collection; next open re-runs `quarantine_stale_hnsw()`. Fast, non-blocking for other endpoints.
- **`scan`** — read-only inspection. Runs `mempalace.repair.scan_palace`, writes `corrupt_ids.txt` next to the palace, returns the count.
- **`prune`** — deletes corrupt IDs via `col.delete()`. The in-library flock (`_palace_write_lock`) already serializes this against concurrent writers.
- **`rebuild`** — destructive: `delete_collection` + `create_collection`. Those backend-level ops are **outside** the `ChromaCollection` flock, so a rebuild racing a concurrent writer silently drops writes. `/repair mode=rebuild` holds every read/write/mine semaphore slot for the rebuild window, and `/silent-save` callers queue to `<palace_parent>/palace-daemon-pending.jsonl` during this window. The queue drains automatically when the rebuild completes.

Only one repair at a time. A second `/repair` call while one is in-flight returns 409.

Check progress with:

    curl http://localhost:8085/repair/status

### /silent-save — Stop-hook save path

    curl -X POST http://localhost:8085/silent-save \
      -H 'Content-Type: application/json' \
      -d '{
        "session_id": "abc-123",
        "wing": "wing_myproject",
        "entry": "CHECKPOINT:2026-04-24|session:abc|msgs:15|recent:...",
        "themes": ["design", "retrieval"],
        "message_count": 15
      }'

Normal response (palace is healthy):

    { "count": 15, "themes": [...], "queued": false,
      "entry_id": "drawer_xyz",
      "systemMessage": "✦ 15 memories woven into the palace — design, retrieval" }

During `/repair mode=rebuild`:

    { "count": 15, "themes": [...], "queued": true,
      "systemMessage": "✦ 15 memories held in trust — the palace is being mended" }

The `systemMessage` field is what Claude Code will render in the terminal when the hook returns it as its `systemMessage` output.

#### Wiring Claude Code hooks through the daemon

To have Stop-hook silent saves go through the daemon (queue-safe during repair, themed messages), set `PALACE_DAEMON_URL` (and `PALACE_API_KEY` if auth is on) in the environment the hook runs under. The fork's `mempalace/hooks_cli.py` detects these, POSTs to `/silent-save`, and emits the daemon's `systemMessage`. If the env var isn't set, or the daemon is unreachable, the hook falls through to the legacy direct-write path — no save is ever lost because the daemon happens to be down.

Example env for the hook invocation (in Claude Code hooks config, or upstream of it):

    PALACE_DAEMON_URL=http://localhost:8085
    PALACE_API_KEY=your-secret     # optional, only if auth is on

### Auth

Pass X-Api-Key: your-secret header on all requests except /health.


## Clients

### Supported tools

| Tool | Config file(s) | Has hooks? |
|---|---|---|
| claude-code | `~/.claude.json` (mcpServers) + `~/.claude/settings.json` (hooks) | Yes (Stop, PreCompact) |
| gemini | `~/.gemini/settings.json` | Yes (SessionStart, SessionEnd, PreCompress) |
| vscode | `~/.vscode/mcp.json` | No |
| cursor | `~/.cursor/mcp.json` | No |
| jetbrains | `~/.config/JetBrains/<IDE>/mcp.json` (Linux) or `~/Library/Application Support/JetBrains/<IDE>/mcp.json` (macOS) | No |

### bootstrap.sh — one-command client setup

`clients/bootstrap.sh` sets up a client machine from scratch: copies `mempalace-mcp.py`
and `hook.py` from Artemis, writes `hook_settings.json`, and patches each tool's config.

**Clients do not need mempalace installed.** Both `hook.py` and `mempalace-mcp.py` are
stdlib-only — only Artemis (the host) needs `pipx install mempalace`.

```bash
# Copy from Artemis and run
scp user@10.0.0.5:/home/user/palace-daemon/clients/bootstrap.sh ~/bootstrap.sh

# Wire a single tool
bash bootstrap.sh --daemon http://10.0.0.5:8085 --tool claude-code

# Wire everything
bash bootstrap.sh --daemon http://10.0.0.5:8085 --tool all
```

`--tool` values: `claude-code` | `gemini` | `vscode` | `cursor` | `jetbrains` | `all`

Files are installed to `~/.local/share/mempalace/`. After running, verify:

```bash
curl http://10.0.0.5:8085/health
```

### hook.py — stdlib hook runner

`clients/hook.py` is a drop-in replacement for `mempalace hook run`. It routes all
operations through palace-daemon instead of accessing the database directly, eliminating
the split-brain risk that existed when `mempalace mine` was spawned as a subprocess.

**Zero dependencies** — pure Python stdlib, no mempalace install needed on clients.

```bash
python3 hook.py --hook stop          --harness claude-code
python3 hook.py --hook precompact    --harness claude-code
python3 hook.py --hook session-start --harness codex
```

#### Behaviour by hook

| Hook | What it does |
|---|---|
| `session-start` | Initialises state dir; seeds the per-session save timestamp; prunes state files older than 7 days |
| `stop` | Three independent save triggers (any one fires): **count** — every 15 exchanges; **time** — every 5 min with unsaved exchanges; **force** — `force_on_stop=true` saves whenever any exchanges are unsaved and ≥`force_min_interval` s have passed (catches short session-end stops). Triggers mine approval block or silent diary save depending on `silent_save`. |
| `precompact` | If `MEMPAL_DIR` set, fires `POST /mine` immediately (no approval — compaction is imminent); passes through |

#### Mine routing

| Old behaviour (`mempalace hook run`) | New behaviour (`hook.py`) |
|---|---|
| Spawns `mempalace mine` as a subprocess | Returns `decision: block` with approval prompt |
| Falls back to transcript dir if `MEMPAL_DIR` unset | No mine triggered if `MEMPAL_DIR` unset |
| Daemon down → still spawns subprocess | Daemon down → passes through silently |

Stop hook mine approval block format:

```
AUTO-INGEST requested (MemPalace).
Target directory: /path/to/dir

Show the user this directory and ask them to approve or deny mining it into the palace.
  Approve → POST {"dir": "/path/to/dir", "mode": "auto"} to http://localhost:8085/mine
  Deny    → inform user, continue.
```

#### Hook settings — `~/.mempalace/hook_settings.json`

| Field | Default | Description |
|---|---|---|
| `daemon_url` | `http://localhost:8085` | URL of palace-daemon; use the LAN IP on remote clients |
| `silent_save` | `true` | If true, auto-saves diary entry via daemon and passes through; if false, blocks and asks the AI to save manually |
| `desktop_toast` | `false` | Fire `notify-send` on save triggers (useful on desktops, skip on SSH) |
| `force_on_stop` | `true` | Save on every Stop where unsaved exchanges exist and ≥`force_min_interval` s have passed — ensures session-end stops are never missed |
| `force_min_interval` | `60` | Minimum seconds between `force_on_stop` saves; prevents a diary write after every single response |

Example (Artemis host):

```json
{
  "silent_save": true,
  "desktop_toast": false,
  "daemon_url": "http://localhost:8085"
}
```

Example (remote client pointing at Artemis):

```json
{
  "silent_save": true,
  "desktop_toast": false,
  "daemon_url": "http://10.0.0.5:8085"
}
```

#### Claude Code hook config (`~/.claude/settings.json`)

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command",
      "command": "python3 /path/to/hook.py --hook stop --harness claude-code",
      "timeout": 30}]}],
    "PreCompact": [{"hooks": [{"type": "command",
      "command": "python3 /path/to/hook.py --hook precompact --harness claude-code",
      "timeout": 60}]}]
  }
}
```

#### Gemini CLI hook config (`~/.gemini/settings.json`)

```json
{
  "hooks": {
    "SessionStart": [{"name": "mempalace-session-start", "type": "command",
      "command": "python3", "args": ["/path/to/hook.py", "--hook", "session-start", "--harness", "gemini-cli"]}],
    "SessionEnd": [{"name": "mempalace-session-stop", "type": "command",
      "command": "python3", "args": ["/path/to/hook.py", "--hook", "stop", "--harness", "gemini-cli"]}],
    "PreCompress": [{"name": "mempalace-precompact", "type": "command",
      "command": "python3", "args": ["/path/to/hook.py", "--hook", "precompact", "--harness", "gemini-cli"],
      "timeout": 30}]
  }
}
```

### mempalace-mcp

`clients/mempalace-mcp.py` bridges any MCP client to palace-daemon over HTTP.
Use this on machines that don't host the palace locally — they talk to the
daemon instead of running mempalace themselves.

**Zero dependencies** — stdlib only, works anywhere Python 3.8+ is installed.

Claude Code setup (`~/.claude.json` → `mcpServers`):

```json
{
  "mempalace": {
    "type": "stdio",
    "command": "python3",
    "args": ["/path/to/clients/mempalace-mcp.py", "--daemon", "http://YOUR_SERVER:8085"],
    "env": {}
  }
}
```

With API key: pass `--api-key your-secret` or set `PALACE_API_KEY` env var.

**Safety First:** If the daemon is unreachable, the client will exit with an error rather than falling back to direct database access. This prevents concurrent access conflicts and ensures stability.

## Testing & Development

To test changes without risking production data or interfering with the primary daemon on port 8085, run a second container against a palace copy.

### Docker (testing + distribution)

Build and run against a palace copy:

```bash
cp -r ~/.mempalace/palace ~/.mempalace/palace-test
docker build -t palace-daemon:latest .
docker run --rm \
  -v ~/.mempalace/palace-test:/palace \
  -p 8086:8085 \
  palace-daemon:latest
```

Or with docker compose (edit `PALACE_PATH` and `PALACE_PORT` in the environment first):

```bash
PALACE_PATH=~/.mempalace/palace-test PALACE_PORT=8086 docker compose up --build
```

The live daemon on port 8085 is never touched. The palace is always mounted as a volume — it is never baked into the image.

### Validation

```bash
curl http://localhost:8086/health
```


## Architecture

    Clients (Claude Code / Android app / netdash / curl)
            |
            v
      palace-daemon (FastAPI)
        ├── _read_sem(PALACE_MAX_READ_CONCURRENCY)   — search, query, stats, …
        ├── _write_sem(PALACE_MAX_WRITE_CONCURRENCY) — add, update, kg mutations, …
        └── _mine_sem(1)                             — bulk import jobs
            |
            v
      mempalace.mcp_server
            |
            v
      ChromaDB / palace files
