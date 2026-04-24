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
1.  **Daemon Lock:** `main.py` uses a file lock (`/tmp/palace-daemon.lock`) to prevent multiple daemon instances from fighting over the database.
2.  **No Client Fallback:** The `mempalace-mcp.py` client is hard-coded to **fail** if it cannot reach the daemon. It will no longer attempt to open the database files directly. This ensures that your MCP client never accidentally creates a "split-brain" scenario where two processes are writing to the same SQLite file.

## Features

- **MCP proxy** — any MCP client connects to /mcp instead of spawning a local process
- **REST API** — search, store, and query the palace over HTTP (Android app, netdash, scripts)
- **Concurrent access control** — three semaphores coordinate reads, writes, and mine jobs; tunable via `PALACE_MAX_CONCURRENCY`
- **Isolated mining** — /mine runs under its own semaphore so bulk imports never stall live traffic
- **Optional API key auth** — set `PALACE_API_KEY` to protect all write endpoints
- **Configurable** — host, port, palace path via CLI args or env vars

## Requirements

- Python 3.12+
- mempalace installed (pipx recommended)

    pip install -r requirements.txt

## Usage

    # Basic start
    python main.py

    # Custom palace path and port
    python main.py --palace ~/.mempalace/palace --port 8085

    # With API key auth
    PALACE_API_KEY=your-secret python main.py

    # Higher concurrency (default: 4 reads, 2 writes)
    PALACE_MAX_CONCURRENCY=8 python main.py


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

## Troubleshooting

### Port 8085 already in use
If the daemon fails to start with `[Errno 98] address already in use`, it usually means a previous instance didn't shut down cleanly.

`palace-daemon.service` includes two `ExecStartPre` guards that run automatically on every start:

    ExecStartPre=-/usr/bin/fuser -k 8085/tcp
    ExecStartPre=-/usr/bin/rm -f /tmp/palace-daemon-8085.lock

The `-` prefix means failures are ignored (i.e. if nothing is blocking, these are no-ops). If running manually without systemd:

    fuser -k 8085/tcp && rm -f /tmp/palace-daemon-8085.lock

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /health | Daemon + palace status (inc. version) |
| POST | /backup | Atomic verified SQLite backup |
| POST | /reload | Clear client cache / refresh index |
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

### Auth

Pass X-Api-Key: your-secret header on all requests except /health.


## Clients

### Supported tools

| Tool | Config file(s) | Has hooks? |
|---|---|---|
| claude-code | `~/.claude.json` (mcpServers) + `~/.claude/settings.json` (hooks) | Yes (Stop, PreCompact) |
| gemini | `~/.gemini/settings.json` | Yes (SessionStart, SessionEnd, PreCompact) |
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
| `session-start` | Initialises state dir; passes through |
| `stop` | Counts exchanges; at every 15th — triggers mine approval block or silent diary save depending on `silent_save` |
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
      "command": "python3", "args": ["/path/to/hook.py", "--hook", "session-start", "--harness", "codex"]}],
    "SessionEnd": [{"name": "mempalace-session-stop", "type": "command",
      "command": "python3", "args": ["/path/to/hook.py", "--hook", "stop", "--harness", "codex"]}],
    "PreCompact": [{"name": "mempalace-precompact", "type": "command",
      "command": "python3", "args": ["/path/to/hook.py", "--hook", "precompact", "--harness", "codex"],
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

To test changes (like auto-healing or stress tests) without risking your production data or interfering with the primary daemon on port 8085, use a **Shadow Palace** via Docker.

### Shadow Palace (Docker)

1. **Clone your data:**
   ```bash
   mkdir -p ~/.mempalace/test_palace
   cp -r ~/.mempalace/palace/* ~/.mempalace/test_palace/
   ```

2. **Run the test container:**
   ```bash
   docker compose -f docker-compose.test.yml up --build -d
   ```

This starts a fully isolated daemon on **port 8086** using your test data. It uses a separate lock file inside the container, ensuring it doesn't collide with your production `palace-daemon-terminal`.

### Validation

Check the health of your test instance:
```bash
curl http://localhost:8086/health
```


## Architecture

    Clients (Claude Code / Android app / netdash / curl)
            |
            v
      palace-daemon (FastAPI)
        ├── _read_sem(N)   — search, query, stats, …
        ├── _write_sem(N/2) — add, update, kg mutations, …
        └── _mine_sem(1)   — bulk import jobs
            |
            v
      mempalace.mcp_server
            |
            v
      ChromaDB / palace files
