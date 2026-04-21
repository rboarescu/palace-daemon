"""
palace-daemon — HTTP/MCP gateway for MemPalace with concurrent access control

Three semaphores govern concurrency (all tunable via PALACE_MAX_CONCURRENCY):
  _read_sem  — up to N concurrent read-only ops (search, query, stats, …)
  _write_sem — up to N//2 concurrent write ops (add, update, kg mutations, …)
  _mine_sem  — one mine job at a time, independent of reads/writes
"""
import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import mempalace.mcp_server as _mp

# ── Config (env vars override CLI defaults) ───────────────────────────────────

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


async def _call(request_dict: dict) -> dict:
    async with _sem_for(request_dict):
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _mp.handle_request, request_dict)
        return result or {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


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
    return {"status": "ok", "daemon": "palace-daemon", "palace": result}


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

    wing = body.get("wing", "general")
    mode = body.get("mode", "convos")
    extract = body.get("extract")
    limit = body.get("limit")

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

def main():
    parser = argparse.ArgumentParser(description="palace-daemon — MemPalace HTTP/MCP gateway")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8085)")
    parser.add_argument("--palace", default=DEFAULT_PALACE, help="Palace path (overrides mempalace config)")
    parser.add_argument("--api-key", default=API_KEY, help="API key for auth (optional)")
    args = parser.parse_args()

    if args.palace:
        os.environ["MEMPALACE_PALACE"] = args.palace
    if args.api_key:
        os.environ["PALACE_API_KEY"] = args.api_key

    uvicorn.run("main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
