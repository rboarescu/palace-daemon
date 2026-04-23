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

### User service (Recommended)

    mkdir -p ~/.config/systemd/user/
    cp palace-daemon.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now palace-daemon

### Global service

    sudo cp palace-daemon.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now palace-daemon

Edit `palace-daemon.service` to set `PALACE_API_KEY` or a custom `--palace` path before installing.

## Troubleshooting

### Port 8085 already in use
If the daemon fails to start with `[Errno 98] address already in use`, it usually means a previous instance didn't shut down cleanly.

The included `palace-daemon.service` uses `ExecStartPre=-/usr/bin/fuser -k 8085/tcp` to automatically clear the port before starting. If running manually, you can clear it with:

    fuser -k 8085/tcp

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
    "command": "/path/to/venv/python",
    "args": ["/path/to/clients/mempalace-mcp.py", "--daemon", "http://YOUR_SERVER:8085"],
    "env": {}
  }
}
```

With API key: pass `--api-key your-secret` or set `PALACE_API_KEY` env var.

**Safety First:** If the daemon is unreachable, the client will exit with an error rather than falling back to direct database access. This prevents concurrent access conflicts and ensures stability.

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
