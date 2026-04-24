"""
palace-daemon — HTTP/MCP gateway for MemPalace with concurrent access control

Three semaphores govern concurrency (all tunable via PALACE_MAX_CONCURRENCY):
  _read_sem  — up to N concurrent read-only ops (search, query, stats, …)
  _write_sem — up to N//2 concurrent write ops (add, update, kg mutations, …)
  _mine_sem  — one mine job at a time, independent of reads/writes

Roadmap:
  [HIGH] Verified Backups: /backup endpoint with integrity_check + smoke test retrieval.
  [DONE] Stability: Auto-detect "Internal Error" during search and trigger index recovery.
  [DONE] Flush: Ensure memories are checkpointed on shutdown and via /flush.
  [HIGH] Unified Routing: Ensure all clients (including miners/compactors) use the Daemon API.
  [MED]  Maintenance: Automate _READ_TOOLS sync with upstream mempalace.
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
import fcntl
import signal
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import mempalace.mcp_server as _mp
from mempalace.backends.chroma import quarantine_stale_hnsw

# ── Config (env vars override CLI defaults) ───────────────────────────────────

VERSION = "1.4.2"
DEFAULT_HOST = os.getenv("PALACE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("PALACE_PORT", "8085"))
DEFAULT_PALACE = os.getenv("PALACE_PATH", "")
API_KEY = os.getenv("PALACE_API_KEY", "")  # read at startup for argparse default; auth checks re-read from env dynamically
PALACE_MAX_CONCURRENCY = int(os.getenv("PALACE_MAX_CONCURRENCY", "4"))

# Read ops: up to N concurrent.
# Regular write ops: up to N//2 concurrent (mempalace ≥3.3.2 is internally safe).
# Mine jobs: exclusive semaphore independent of reads/writes so a long mine
# doesn't starve normal traffic.
_read_sem = asyncio.Semaphore(PALACE_MAX_CONCURRENCY)
_write_sem = asyncio.Semaphore(max(1, PALACE_MAX_CONCURRENCY // 2))
_mine_sem = asyncio.Semaphore(1)

# Tools that only read state — everything else is treated as a write.
_READ_TOOLS = {
    "mempalace_search",
    "mempalace_kg_query",
    "mempalace_kg_stats",
    "mempalace_kg_timeline",
    "mempalace_graph_stats",
    "mempalace_status",
    "mempalace_list_drawers",
    "mempalace_get_drawer",
    "mempalace_list_rooms",
    "mempalace_list_wings",
    "mempalace_list_tunnels",
    "mempalace_find_tunnels",
    "mempalace_follow_tunnels",
    "mempalace_traverse",
    "mempalace_diary_read",
    "mempalace_check_duplicate",
    "mempalace_get_taxonomy",
    "mempalace_get_aaak_spec",
    "mempalace_hook_settings",
}


def _check_auth(x_api_key: str | None):
    key = os.getenv("PALACE_API_KEY", "")
    if key and x_api_key != key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _sem_for(request_dict: dict) -> asyncio.Semaphore:
    method = request_dict.get("method", "")
    if method == "ping":
        return _read_sem
    tool_name = request_dict.get("params", {}).get("name", "")
    return _read_sem if tool_name in _READ_TOOLS else _write_sem


async def _auto_repair():
    """Trigger index recovery and reload the mempalace client."""
    import logging
    logger = logging.getLogger(__name__)
    
    loop = asyncio.get_running_loop()
    palace_path = _mp._config.palace_path
    moved = await loop.run_in_executor(None, quarantine_stale_hnsw, palace_path)
    if moved:
        logger.warning("AUTO-REPAIR: Quarantined %d stale HNSW segments. Reloading client.", len(moved))
        # Clear client cache to force a fresh PersistentClient (which triggers rebuild)
        _mp._client_cache = None
        _mp._collection_cache = None
        return len(moved)
    
    logger.info("AUTO-REPAIR: No stale segments found during scan.")
    return 0


async def _call(request_dict: dict, retry_on_hnsw: bool = True) -> dict:
    async with _sem_for(request_dict):
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, _mp.handle_request, request_dict)
            
            if result and "error" in result:
                msg = str(result["error"].get("message", ""))
                is_hnsw_error = "Internal error: Error finding id" in msg or "Internal error: id" in msg
                
                tool_name = request_dict.get("params", {}).get("name", "")
                if is_hnsw_error and retry_on_hnsw and tool_name in _READ_TOOLS:
                    # Auto-repair and retry ONCE (write ops are excluded: retrying risks duplicate drawers)
                    repaired_count = await _auto_repair()
                    if repaired_count > 0:
                        return await loop.run_in_executor(None, _mp.handle_request, request_dict)

                    result["error"]["message"] += " (Daemon hint: HNSW index stale. Auto-repair attempted but index might still be inconsistent)"
                elif is_hnsw_error and tool_name not in _READ_TOOLS:
                    result["error"]["message"] += " (Daemon hint: HNSW error on write op — manual /reload may be needed)"
            return result or {}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": request_dict.get("id"), "error": {"code": -32000, "message": str(e)}}


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    logger = logging.getLogger(__name__)
    
    # Register signal handlers for graceful shutdown
    def handle_exit(sig, frame):
        logger.warning("Received signal %s, shutting down...", sig)
        # _mp handles its own state, but we ensure the daemon stops accepting new tasks
        sys.exit(0)
    
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    moved = quarantine_stale_hnsw(_mp._config.palace_path)
    if moved:
        logger.warning(
            "Quarantined %d stale HNSW segment(s) — ChromaDB will rebuild indexes: %s",
            len(moved), moved,
        )
    
    yield
    
    # --- Shutdown: Silent Save / Flush ---
    logger.info("Lifespan: shutting down, flushing memories...")
    try:
        # We call mempalace_memories_filed_away which triggers a checkpoint in recent mempalace versions
        await _call({
            "jsonrpc": "2.0", "id": "shutdown",
            "method": "tools/call",
            "params": {"name": "mempalace_memories_filed_away", "arguments": {}}
        }, retry_on_hnsw=False)
        logger.info("Flush complete.")
    except Exception as e:
        logger.error("Error during shutdown flush: %s", e)


app = FastAPI(title="palace-daemon", lifespan=lifespan)


# ── MCP proxy ─────────────────────────────────────────────────────────────────

@app.post("/mcp")
async def mcp_proxy(request: Request, x_api_key: str | None = Header(default=None)) -> JSONResponse:
    _check_auth(x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    response = await _call(body)
    return JSONResponse(content=response)


# ── REST convenience endpoints ────────────────────────────────────────────────

@app.get("/health")
async def health():
    # Bypass semaphores — health must respond even when all slots are busy.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _mp.handle_request, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}) or {}
    return {"status": "ok", "daemon": "palace-daemon", "version": VERSION, "palace": result}


@app.get("/search")
async def search(q: str, limit: int = 5, x_api_key: str | None = Header(default=None)):
    _check_auth(x_api_key)
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": {"query": q, "max_results": limit}},
    })
    return _unwrap(result)


@app.get("/context")
async def context(topic: str, limit: int = 5, x_api_key: str | None = Header(default=None)):
    # Alias for /search with a semantically friendlier name for LLM tool prompts
    _check_auth(x_api_key)
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": {"query": topic, "max_results": limit}},
    })
    return _unwrap(result)


@app.post("/memory")
async def store_memory(request: Request, x_api_key: str | None = Header(default=None)):
    _check_auth(x_api_key)
    body = await request.json()
    content = body.get("content", "")
    wing = body.get("wing", "general")
    room = body.get("room", "notes")
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "mempalace_add_drawer",
            "arguments": {"wing": wing, "room": room, "content": content},
        },
    })
    return _unwrap(result)


@app.get("/stats")
async def stats(x_api_key: str | None = Header(default=None)):
    _check_auth(x_api_key)
    tools = ["mempalace_kg_stats", "mempalace_graph_stats", "mempalace_status"]
    responses = await asyncio.gather(*[
        _call({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": t, "arguments": {}}})
        for i, t in enumerate(tools, 1)
    ])
    kg, graph, status = [_unwrap(r) for r in responses]
    return {"kg": kg, "graph": graph, "status": status}


@app.post("/flush")
async def flush_palace(x_api_key: str | None = Header(default=None)):
    """Manually trigger a checkpoint/flush of memories to disk."""
    _check_auth(x_api_key)
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_memories_filed_away", "arguments": {}},
    })
    return _unwrap(result)


@app.post("/reload")
async def reload_palace(x_api_key: str | None = Header(default=None)):
    """Force the daemon to reconnect to the database and refresh its index."""
    _check_auth(x_api_key)
    # _mp._get_client uses a cache; we clear it to force a fresh PersistentClient
    _mp._client_cache = None; _mp._collection_cache = None
    return {"status": "reloaded", "message": "Palace client cache cleared"}


@app.post("/backup")
async def create_backup(x_api_key: str | None = Header(default=None)):
    """
    Perform a verified atomic backup of the palace database.
    Uses sqlite3 .backup to ensure consistency even under load.
    """
    _check_auth(x_api_key)
    palace_path = _mp._config.palace_path
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    
    backup_dir = os.path.join(os.path.dirname(palace_path), "palace.backup")
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"chroma.sqlite3.{timestamp}.bak")

    # Hold the write semaphore so no daemon-driven writes race the backup start.
    async with _write_sem:
        try:
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(backup_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

            check = sqlite3.connect(backup_path)
            try:
                cursor = check.cursor()
                cursor.execute("PRAGMA integrity_check;")
                status = cursor.fetchone()[0]
            finally:
                check.close()

            if status != "ok":
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                raise Exception(f"Integrity check failed: {status}")

            return {
                "status": "success",
                "backup_file": backup_path,
                "integrity": status,
                "timestamp": timestamp
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


# ── Mine endpoint (serialized bulk import) ────────────────────────────────────

@app.post("/mine")
async def mine(request: Request, x_api_key: str | None = Header(default=None)):
    """
    Run mempalace mine under _mine_sem (one job at a time). Normal read/write
    traffic continues unblocked during the job; mempalace ≥3.3.2 enforces
    its own mine lock at the library level.

    Body: { "dir": "/path/to/files", "wing": "general", "mode": "convos",
            "extract": "exchange", "limit": 100 }
    """
    _check_auth(x_api_key)
    body = await request.json()
    directory = body.get("dir")
    if not directory:
        raise HTTPException(status_code=400, detail="'dir' is required")

    dir_path = Path(directory)
    if not dir_path.is_absolute() or ".." in dir_path.parts:
        raise HTTPException(status_code=400, detail="'dir' must be an absolute path with no traversal")
    if not dir_path.exists():
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {directory}")
    if not dir_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {directory}")

    wing = body.get("wing", "general")
    mode = body.get("mode", "convos")
    extract = body.get("extract")
    limit = body.get("limit")

    _VALID_MODES = {"convos", "projects"}
    _VALID_EXTRACTS = {"exchange", "general"}
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"'mode' must be one of: {', '.join(_VALID_MODES)}")
    if extract is not None and extract not in _VALID_EXTRACTS:
        raise HTTPException(status_code=400, detail=f"'extract' must be one of: {', '.join(_VALID_EXTRACTS)}")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'limit' must be an integer")

    mempalace_bin = os.path.join(os.path.dirname(sys.executable), "mempalace")
    cmd = [mempalace_bin, "mine", directory, "--mode", mode, "--wing", wing]
    if extract:
        cmd += ["--extract", extract]
    if limit:
        cmd += ["--limit", str(limit)]

    async with _mine_sem:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _unwrap(mcp_response: dict) -> Any:
    try:
        text = mcp_response["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, TypeError, json.JSONDecodeError):
        return mcp_response


# ── Entry point ───────────────────────────────────────────────────────────────

# Global to prevent GC from closing the file and releasing the lock
_lock_file = None


def main():
    global _lock_file
    parser = argparse.ArgumentParser(description="palace-daemon — MemPalace HTTP/MCP gateway")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8085)")
    parser.add_argument("--palace", default=DEFAULT_PALACE, help="Palace path (overrides mempalace config)")
    parser.add_argument("--api-key", default=API_KEY, help="API key for auth (optional)")
    args = parser.parse_args()

    # Simple file lock to prevent multiple daemon instances on the same port.
    # Use ~/.cache/palace-daemon/ (mode 0o700) instead of /tmp to avoid world-writable exposure.
    lock_dir = Path.home() / ".cache" / "palace-daemon"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file_path = str(lock_dir / f"daemon-{args.port}.lock")
    _lock_file = open(lock_file_path, "w")
    try:
        fcntl.lockf(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print(f"ERROR: Another instance of palace-daemon is already running on port {args.port}.", file=sys.stderr)
        sys.exit(1)

    if args.palace:
        os.environ["MEMPALACE_PALACE"] = args.palace
    if args.api_key:
        os.environ["PALACE_API_KEY"] = args.api_key

    uvicorn.run("main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
