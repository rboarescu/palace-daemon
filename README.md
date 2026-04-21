# palace-daemon

An HTTP/MCP gateway for [MemPalace](https://github.com/MemPalace/mempalace) that serializes all ChromaDB access through a single process, preventing concurrent write corruption.

## Why

MemPalace stores memories in ChromaDB (SQLite). When multiple clients write simultaneously — an AI agent, an Android app, a bulk import job — SQLite corrupts. palace-daemon fixes this by funnelling every operation through one asyncio.Lock().

## Features

- **MCP proxy** — any MCP client connects to /mcp instead of spawning a local process
- **REST API** — search, store, and query the palace over HTTP (Android app, netdash, scripts)
- **Serialized mining** — /mine endpoint runs mempalace mine under the global lock, so bulk imports never race with live queries
- **Optional API key auth** — set PALACE_API_KEY to protect all write endpoints
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


## Security

> **Do not expose port 8085 to the internet without setting .**
> The  endpoint accepts arbitrary filesystem paths — anyone with access
> can trigger reads from any directory on your server.

For local network use, leaving auth disabled is fine. For remote access, always set an API key:

    PALACE_API_KEY=your-secret python main.py

## systemd

    sudo cp palace-daemon.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now palace-daemon

Edit palace-daemon.service to set PALACE_API_KEY or a custom --palace path before installing.

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /health | Daemon + palace status |
| GET | /stats | Wing/room counts, KG stats |
| GET | /search?q=...&limit=5 | Semantic search |
| GET | /context?topic=... | Same as search, named for LLM use |
| POST | /memory | Store a drawer {content, wing, room} |
| POST | /mcp | Full MCP JSON-RPC proxy |
| POST | /mine | Bulk import under lock |

### /mine — serialized bulk import

    curl -X POST http://localhost:8085/mine       -H 'Content-Type: application/json'       -d '{"dir": "/path/to/files", "wing": "gemini", "mode": "convos"}'

Body: dir (required), wing, mode (projects/convos), extract (exchange/general), limit.

All other requests queue behind the lock while mining runs.

### Auth

Pass X-Api-Key: your-secret header on all requests except /health.


## Clients

### mempalace-mcp

`clients/mempalace-mcp.py` bridges any MCP client to palace-daemon over HTTP.
Use this on machines that don't host the palace locally — they talk to the
daemon instead of running mempalace themselves.

**Zero dependencies** — stdlib only, works anywhere Python 3.8+ is installed.

Claude Code setup (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "python",
      "args": ["/path/to/clients/mempalace-mcp.py", "--daemon", "http://YOUR_SERVER:8085"]
    }
  }
}
```

With API key: pass `--api-key your-secret` or set `PALACE_API_KEY` env var.

## Architecture

    Clients (Claude Code / Android app / netdash / curl)
            |
            v
      palace-daemon (FastAPI + asyncio.Lock)
            |  single writer
            v
      ChromaDB (SQLite)  <--  mempalace mine (via /mine endpoint)
