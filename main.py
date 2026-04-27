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
import logging
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
from mempalace import repair as _mp_repair
from mempalace.backends.chroma import quarantine_stale_hnsw

import messages

# ── Config (env vars override CLI defaults) ───────────────────────────────────

VERSION = "1.5.1"
DEFAULT_HOST = os.getenv("PALACE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("PALACE_PORT", "8085"))
DEFAULT_PALACE = os.getenv("PALACE_PATH", "")
API_KEY = os.getenv("PALACE_API_KEY", "")  # read at startup for argparse default; auth checks re-read from env dynamically
PALACE_MAX_CONCURRENCY = int(os.getenv("PALACE_MAX_CONCURRENCY", "4"))
PALACE_MAX_READ_CONCURRENCY = int(os.getenv("PALACE_MAX_READ_CONCURRENCY", str(PALACE_MAX_CONCURRENCY)))
PALACE_MAX_WRITE_CONCURRENCY = int(os.getenv("PALACE_MAX_WRITE_CONCURRENCY", str(max(1, PALACE_MAX_CONCURRENCY // 2))))

# Canonical topic for Stop-hook auto-save checkpoint diary entries. Matches
# the value already used in clients/hook.py and clients/mempal-fast.py, plus
# mempalace's `tool_diary_write` topic-routing for the
# `mempalace_session_recovery` collection (the structural fix for #1161-era
# checkpoint domination of search results).
CHECKPOINT_TOPIC = "checkpoint"
# Legacy synonyms older clients may have written. `_canonical_topic()`
# rewrites these at the daemon boundary with a warning log line so drift
# is visible and the palace stays clean.
CHECKPOINT_TOPIC_SYNONYMS = ("auto-save",)

# Read ops: up to PALACE_MAX_READ_CONCURRENCY concurrent.
# Write ops: up to PALACE_MAX_WRITE_CONCURRENCY concurrent.
# Set PALACE_MAX_WRITE_CONCURRENCY=1 to serialise writes (mitigates MemPalace
# issue #1161 — HNSW num_threads not persisted in ChromaDB 1.5.x).
# Mine jobs: exclusive semaphore independent of reads/writes so a long mine
# doesn't starve normal traffic.
_read_sem = asyncio.Semaphore(PALACE_MAX_READ_CONCURRENCY)
_write_sem = asyncio.Semaphore(PALACE_MAX_WRITE_CONCURRENCY)
_mine_sem = asyncio.Semaphore(1)

# Repair state — when in_progress is True, /silent-save queues instead of writing.
# The fast-path check is lock-free (single-assignment dict); _repair_lock serializes
# start/end transitions and prevents overlapping repairs.
_repair_state: dict[str, Any] = {"in_progress": False, "mode": None, "started_at": None}
_repair_lock = asyncio.Lock()

_log = logging.getLogger("palace-daemon")


# ── Systemd watchdog / sd_notify ─────────────────────────────────────────────

def _sd_notify(msg: str) -> None:
    """Send a message to systemd notify socket without external dependencies."""
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    try:
        import socket as _sock
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as s:
            # Abstract namespace sockets use NUL prefix; systemd uses @ prefix.
            addr = chr(0) + sock_path[1:] if sock_path.startswith("@") else sock_path
            s.sendto(msg.encode(), addr)
    except Exception:
        pass


def _watchdog_interval() -> int:
    """Return WatchdogSec in seconds from WATCHDOG_USEC (set by systemd), or 0."""
    try:
        return int(os.environ.get("WATCHDOG_USEC", "0")) // 1_000_000
    except ValueError:
        return 0


async def _watchdog_loop(interval_secs: int) -> None:
    """Ping systemd watchdog at half the watchdog interval, only when palace is healthy."""
    tick = max(10, interval_secs // 2)
    while True:
        await asyncio.sleep(tick)
        try:
            loop = asyncio.get_running_loop()
            col = await loop.run_in_executor(None, _mp._get_collection)
            if col is not None:
                _sd_notify("WATCHDOG=1\n")
            else:
                _log.warning("Watchdog: palace collection unavailable — skipping WATCHDOG=1")
        except Exception as e:
            _log.warning("Watchdog check failed: %s", e)


async def _warn_if_hnsw_threads_unset() -> None:
    """Warn if hnsw:num_threads != 1 after a collection reopen.

    ChromaDB 1.5.x does not persist HNSW metadata across reopens (MemPalace
    issue #1161). After any cache clear the collection silently reverts to
    parallel inserts, risking SIGSEGV under concurrent writes.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _mp.handle_request, {
            "jsonrpc": "2.0", "id": "hnsw-check", "method": "ping", "params": {}
        })
        col = _mp._collection_cache
        meta = (col and getattr(col, "_collection", None) and
                getattr(col._collection, "metadata", None)) or {}
        threads = meta.get("hnsw:num_threads")
        if threads != 1:
            _log.warning(
                "HNSW num_threads=%s after collection reopen — parallel inserts active. "
                "Concurrent writes risk SIGSEGV. See MemPalace issue #1161. "
                "Upgrade to mempalace >=3.3.4 when available.",
                threads,
            )
    except Exception:
        pass


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
    loop = asyncio.get_running_loop()
    palace_path = _mp._config.palace_path
    moved = await loop.run_in_executor(None, quarantine_stale_hnsw, palace_path)
    if moved:
        _log.warning("AUTO-REPAIR: Quarantined %d stale HNSW segments. Reloading client.", len(moved))
        _mp._client_cache = None
        _mp._collection_cache = None
        await _warn_if_hnsw_threads_unset()
        return len(moved)
    _log.info("AUTO-REPAIR: No stale segments found during scan.")
    return 0


# ── Exclusive palace context (for rebuild) ───────────────────────────────────

@asynccontextmanager
async def _exclusive_palace():
    """Acquire every semaphore slot — no daemon-mediated work runs until release.

    Used by /repair mode=rebuild, which deletes and recreates the collection
    (backend-level, outside the ChromaCollection flock). Any in-flight write
    would race with the delete/create and be lost. Holding every slot makes
    sure nothing daemon-mediated is mid-flight when the rebuild starts.
    """
    read_slots = PALACE_MAX_CONCURRENCY
    write_slots = max(1, PALACE_MAX_CONCURRENCY // 2)
    r_held = 0
    w_held = 0
    m_held = False
    try:
        for _ in range(read_slots):
            await _read_sem.acquire()
            r_held += 1
        for _ in range(write_slots):
            await _write_sem.acquire()
            w_held += 1
        await _mine_sem.acquire()
        m_held = True
        yield
    finally:
        if m_held:
            _mine_sem.release()
        for _ in range(w_held):
            _write_sem.release()
        for _ in range(r_held):
            _read_sem.release()


# ── Pending-writes queue (held during rebuild) ───────────────────────────────

def _pending_writes_path() -> str:
    """Location of the jsonl queue that holds silent-saves during rebuild."""
    palace_path = _mp._config.palace_path
    parent = os.path.dirname(palace_path.rstrip("/")) or os.path.expanduser("~")
    return os.path.join(parent, "palace-daemon-pending.jsonl")


async def _enqueue_pending_write(payload: dict) -> None:
    """Append a silent-save payload to the pending-writes queue (off-loop)."""
    path = _pending_writes_path()
    line = json.dumps({"payload": payload, "enqueued_at": datetime.now().isoformat()})

    def _append():
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_append)


async def _drain_pending_writes() -> int:
    """Replay queued silent-saves after a rebuild completes.

    Rename-then-read so a concurrent /silent-save appending after the rename
    lands in a fresh pending file, not the one we're draining. Each entry is
    replayed under _write_sem to honour _do_silent_save_write's contract.
    Failed entries are quarantined to a timestamped file so the next drain
    pass doesn't replay successful saves.
    """
    path = _pending_writes_path()
    if not os.path.isfile(path):
        return 0
    proc_path = path + ".processing"
    try:
        os.rename(path, proc_path)
    except OSError:
        return 0
    count = 0
    failed_lines: list[str] = []
    try:
        with open(proc_path, encoding="utf-8") as f:
            lines = [ln for ln in f.readlines() if ln.strip()]
        for line in lines:
            try:
                entry = json.loads(line)
                async with _write_sem:
                    result = await _do_silent_save_write(entry["payload"])
                if result.get("success"):
                    count += 1
                else:
                    _log.warning("drain: replay failed: %s", result.get("error"))
                    failed_lines.append(line)
            except Exception:
                _log.exception("drain: entry replay raised")
                failed_lines.append(line)
        if failed_lines:
            qpath = proc_path + ".failed-" + datetime.now().strftime("%Y%m%d%H%M%S")
            with open(qpath, "w", encoding="utf-8") as f:
                f.writelines(failed_lines)
            _log.warning("drain: %d entries quarantined at %s", len(failed_lines), qpath)
        os.remove(proc_path)
    except Exception:
        _log.exception("drain: read failed; leaving %s in place", proc_path)
    return count


def _canonical_topic(topic) -> str:
    """Canonicalize a Stop-hook checkpoint topic at the daemon boundary.

    Synonyms become ``CHECKPOINT_TOPIC`` with a warning log so client-side
    drift is visible. Any other string is left as-is — the caller may have
    legitimately used a non-checkpoint topic name on this diary write
    (e.g. ``"musings"``, ``"decisions"``) and we shouldn't clobber that.

    Non-string inputs (``None``, numbers, lists from a malformed JSON
    payload) collapse to ``CHECKPOINT_TOPIC`` with a warning, rather than
    leaking through to ``tool_diary_write`` and turning a bad client
    request into an internal type error.

    Defense in depth: the bundled clients already write the canonical
    value; this rewrite catches future drift, third-party callers, or
    buggy clients without rejecting the save outright.
    """
    if not isinstance(topic, str):
        _log.warning(
            "silent-save: non-string topic %r (%s); coercing to %r",
            topic, type(topic).__name__, CHECKPOINT_TOPIC,
        )
        return CHECKPOINT_TOPIC
    if topic in CHECKPOINT_TOPIC_SYNONYMS:
        _log.warning(
            "silent-save: rewriting non-canonical checkpoint topic %r → %r",
            topic, CHECKPOINT_TOPIC,
        )
        return CHECKPOINT_TOPIC
    return topic


async def _do_silent_save_write(payload: dict) -> dict:
    """Write a diary checkpoint via tool_diary_write in an executor.

    Caller is expected to hold _write_sem. Returns mempalace's raw dict
    (typically {"success": True, "entry_id": ...} or {"success": False, "error": ...}).
    """
    wing = payload.get("wing", "") or ""
    entry = payload.get("entry", "")
    topic = _canonical_topic(payload.get("topic", CHECKPOINT_TOPIC))
    agent_name = payload.get("agent_name", "session-hook")
    loop = asyncio.get_running_loop()

    def _work():
        from mempalace.mcp_server import tool_diary_write
        return tool_diary_write(
            agent_name=agent_name,
            entry=entry,
            topic=topic,
            wing=wing,
        )

    try:
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        return {"success": False, "error": str(e)}


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
    
    # Uvicorn installs its own SIGINT/SIGTERM handlers that shut down gracefully;
    # we don't need to override them. Calling sys.exit() from inside an asyncio
    # signal handler tears the event loop down mid-coroutine and skips lifespan
    # shutdown (the flush). Leave signal handling to uvicorn.

    moved = quarantine_stale_hnsw(_mp._config.palace_path)
    if moved:
        logger.warning(
            "Quarantined %d stale HNSW segment(s) — ChromaDB will rebuild indexes: %s",
            len(moved), moved,
        )

    # Warm the ChromaDB client before accepting traffic. The Rust HNSW binding
    # occasionally segfaults on the very first request if opened cold; opening
    # it here (before yield) ensures the PersistentClient is fully initialized.
    # We open the collection directly (not via ping) so that _get_collection's
    # hnsw:num_threads=1 fix is applied before _warn_if_hnsw_threads_unset runs.
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _mp._get_collection, True)
        logger.info("Palace client warmed up.")
    except Exception as e:
        logger.warning("Warmup collection open failed (non-fatal): %s", e)
    await _warn_if_hnsw_threads_unset()

    # Signal systemd that startup is complete (Type=notify in service file).
    _sd_notify("READY=1\n")

    # Start systemd watchdog loop if WatchdogSec is configured.
    wdog_secs = _watchdog_interval()
    if wdog_secs > 0:
        asyncio.create_task(_watchdog_loop(wdog_secs))
        logger.info("Systemd watchdog active (interval=%ds, tick=%ds).", wdog_secs, max(10, wdog_secs // 2))

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
    # Test actual collection access so /health reflects true palace state.
    palace_ok = False
    try:
        col = await loop.run_in_executor(None, _mp._get_collection)
        palace_ok = col is not None
    except Exception:
        pass
    status = "ok" if palace_ok else "degraded"
    payload = {"status": status, "daemon": "palace-daemon", "version": VERSION, "palace": result}
    if not palace_ok:
        return JSONResponse(content=payload, status_code=503)
    return payload


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
    unwrapped = _unwrap(result)
    if isinstance(unwrapped, dict) and unwrapped.get('success'):
        unwrapped['toast'] = f'Filed to {wing}/{room}'
    return unwrapped


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


# ── Repair + silent-save ─────────────────────────────────────────────────────

@app.post("/silent-save")
async def silent_save(request: Request, x_api_key: str | None = Header(default=None)):
    """
    Silent Stop-hook save path. Writes a diary checkpoint during normal ops;
    during /repair mode=rebuild, queues the payload to a jsonl file and
    returns a themed "held in trust" message. The queue drains automatically
    when the rebuild completes.

    Body: {
      session_id, wing, entry, topic?, agent_name?,
      themes?: [...],       # for the returned systemMessage tag
      message_count?: int,  # count the hook wants displayed (often len(messages))
    }
    """
    _check_auth(x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not body.get("entry"):
        raise HTTPException(status_code=400, detail="'entry' is required")

    themes = body.get("themes") or []
    raw_msg_count = body.get("message_count")
    if raw_msg_count is None:
        msg_count = 1
    else:
        try:
            msg_count = int(raw_msg_count)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="'message_count' must be an integer",
            )
        if msg_count <= 0:
            msg_count = 1

    # Acquire write slot, check rebuild flag under lock, then write or queue.
    # Queue only when /repair is doing a rebuild — other modes (light/scan/
    # prune) don't replace the collection out from under in-flight writes.
    async with _write_sem:
        if (
            _repair_state["in_progress"]
            and _repair_state.get("mode") == "rebuild"
        ):
            await _enqueue_pending_write(body)
            return {
                "count": msg_count,
                "themes": themes,
                "queued": True,
                "systemMessage": messages.save_queued(msg_count, themes),
            }
        result = await _do_silent_save_write(body)

    if result.get("success"):
        return {
            "count": msg_count,
            "themes": themes,
            "queued": False,
            "entry_id": result.get("entry_id"),
            "toast": f"Palace updated: {msg_count} msgs saved ({themes[0] if themes else "checkpoint"})",
            "systemMessage": messages.save_ok(msg_count, themes),
        }
    raise HTTPException(
        status_code=500,
        detail=f"silent save failed: {result.get('error', 'unknown')}",
    )


@app.post("/repair")
async def repair(request: Request, x_api_key: str | None = Header(default=None)):
    """
    Coordinate a repair with daemon-mediated traffic.

    Body: { "mode": "light" | "scan" | "prune" | "rebuild" }
      light   — clear caches; next client open re-runs quarantine_stale_hnsw(). Cheap.
      scan    — find corrupt IDs, write corrupt_ids.txt. Read-only.
      prune   — delete corrupt IDs via the flock-safe col.delete path.
      rebuild — destructive: delete + recreate the collection. Holds every
                semaphore slot; silent-save queues during this window and
                drains automatically on completion.

    Only one repair at a time. Second call while one is in-flight → 409.
    """
    _check_auth(x_api_key)
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    mode = (body.get("mode") or "light").lower()
    if mode not in ("light", "scan", "prune", "rebuild"):
        raise HTTPException(
            status_code=400,
            detail="mode must be one of: light, scan, prune, rebuild",
        )

    # Start transition — guarded so two /repair callers can't both begin.
    async with _repair_lock:
        if _repair_state["in_progress"]:
            raise HTTPException(
                status_code=409,
                detail=f"repair already in progress (mode={_repair_state['mode']})",
            )
        _repair_state["in_progress"] = True
        _repair_state["mode"] = mode
        _repair_state["started_at"] = datetime.now().isoformat()

    _log.info(messages.repair_begin(mode))
    start = datetime.now()
    result: dict[str, Any] = {}
    drained = 0

    try:
        if mode == "light":
            # Clear cached client + collection. Next touch will re-open and
            # re-run quarantine_stale_hnsw() via make_client().
            async with _write_sem:
                _mp._client_cache = None
                _mp._collection_cache = None
                result = {"caches_cleared": True}
            await _warn_if_hnsw_threads_unset()

        elif mode == "scan":
            # Read-only: cap at read slot.
            async with _read_sem:
                loop = asyncio.get_running_loop()
                palace_path = _mp._config.palace_path
                await loop.run_in_executor(None, _mp_repair.scan_palace, palace_path)
                corrupt_file = os.path.join(palace_path, "corrupt_ids.txt")
                count = 0
                if os.path.isfile(corrupt_file):
                    with open(corrupt_file, encoding="utf-8") as f:
                        count = sum(1 for ln in f if ln.strip())
                result = {"corrupt_ids_found": count, "corrupt_file": corrupt_file}

        elif mode == "prune":
            # Takes flock internally (col.delete). Hold a single write slot so
            # we don't inflate daemon throughput while repair is running.
            async with _write_sem:
                loop = asyncio.get_running_loop()
                palace_path = _mp._config.palace_path
                await loop.run_in_executor(
                    None,
                    lambda: _mp_repair.prune_corrupt(palace_path=palace_path, confirm=True),
                )
                result = {"pruned": True}
            _mp._client_cache = None
            _mp._collection_cache = None
            await _warn_if_hnsw_threads_unset()

        elif mode == "rebuild":
            # Destructive: deletes + recreates the collection. Hold every
            # semaphore slot so no daemon-mediated write races the swap.
            async with _exclusive_palace():
                loop = asyncio.get_running_loop()
                palace_path = _mp._config.palace_path
                await loop.run_in_executor(None, _mp_repair.rebuild_index, palace_path)
                _mp._client_cache = None
                _mp._collection_cache = None
                result = {"rebuilt": True}
            await _warn_if_hnsw_threads_unset()

    except Exception as e:
        _log.exception("repair (%s) failed", mode)
        async with _repair_lock:
            _repair_state["in_progress"] = False
            _repair_state["mode"] = None
            _repair_state["started_at"] = None
        raise HTTPException(status_code=500, detail=f"repair failed: {e}")

    # Clear the flag BEFORE draining so replayed silent-saves go direct,
    # not back into the queue.
    async with _repair_lock:
        _repair_state["in_progress"] = False
        _repair_state["mode"] = None
        _repair_state["started_at"] = None

    if mode == "rebuild":
        drained = await _drain_pending_writes()

    duration = (datetime.now() - start).total_seconds()
    _log.info(messages.repair_complete(mode, drained, duration))
    return {
        "mode": mode,
        "result": result,
        "drained": drained,
        "duration_s": round(duration, 3),
        "systemMessage": messages.repair_complete(mode, drained, duration),
    }


@app.get("/repair/status")
async def repair_status():
    """Current repair state + pending-writes queue depth."""
    queue_path = _pending_writes_path()
    pending = 0
    if os.path.isfile(queue_path):
        try:
            with open(queue_path, encoding="utf-8") as f:
                pending = sum(1 for ln in f if ln.strip())
        except OSError:
            pending = -1
    return {
        "in_progress": _repair_state["in_progress"],
        "mode": _repair_state["mode"],
        "started_at": _repair_state["started_at"],
        "pending_writes": pending,
        "pending_writes_path": queue_path,
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


def _clear_port(port: int):
    """Attempt to kill any process currently holding the target port."""
    import subprocess
    try:
        # Use fuser to kill the process on the port.
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    except Exception:
        pass


def main():
    global _lock_file
    parser = argparse.ArgumentParser(description="palace-daemon — MemPalace HTTP/MCP gateway")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8085)")
    parser.add_argument("--palace", default=DEFAULT_PALACE, help="Palace path (overrides mempalace config)")
    parser.add_argument("--api-key", default=API_KEY, help="API key for auth (optional)")
    parser.add_argument("--force", action="store_true", help="Force clear port before starting (used by systemd)")
    parser.add_argument("--manual", action="store_true", help="Allow manual start outside of systemd")
    args = parser.parse_args()

    # Prevent accidental manual starts by agents
    if not os.getenv("INVOCATION_ID") and not args.manual:
        print("ERROR: Manual startup detected. Use 'sudo systemctl start palace-daemon' instead.")
        print("If you MUST run manually for debugging, use the --manual flag.")
        sys.exit(1)

    if args.force:
        _clear_port(args.port)

    # Simple file lock to prevent multiple daemon instances on the same port.
    # ~/.cache/palace-daemon/ (mode 0o700) avoids world-writable /tmp exposure.
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
