#!/usr/bin/env python3
"""
palace-daemon hook runner — stdlib-only replacement for `mempalace hook run`.

Routes all mine operations through palace-daemon (POST /mine).
Never spawns mempalace as a subprocess or accesses the database directly.
If the daemon is unreachable, passes through silently — no fallback to direct access.

Mine operations require explicit user approval via a block response before firing.
MEMPAL_DIR env var controls what directory to mine; if unset, no mine is triggered.

Usage:
    python3 hook.py --hook stop        --harness claude-code
    python3 hook.py --hook precompact  --harness claude-code
    python3 hook.py --hook session-start --harness codex

Settings: ~/.mempalace/hook_settings.json
    daemon_url        (default: http://localhost:8085)
    silent_save       (default: true)  — pass through after diary save; false = block for manual save
    desktop_toast     (default: false) — fire notify-send on save triggers
    force_on_stop     (default: true)  — save at session end even with few exchanges (≥FORCE_MIN_INTERVAL s between saves)
    force_min_interval (default: 60)   — minimum seconds between force_on_stop saves
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SAVE_INTERVAL = 15           # count-based: save every N exchanges
TIME_SAVE_INTERVAL = 300     # time-based: save if this many seconds elapsed with unsaved exchanges
FORCE_MIN_INTERVAL = 60      # force_on_stop: minimum seconds between saves (prevents per-response spam)
CHECKPOINT_TOPIC = "checkpoint"  # keep in sync with main.py and mempal-fast.py — used by kind= search filter
STATE_DIR = Path.home() / ".mempalace" / "hook_state"
HOOK_SETTINGS_PATH = Path.home() / ".mempalace" / "hook_settings.json"

SUPPORTED_HARNESSES = {"claude-code", "codex", "gemini-cli"}

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint (MemPalace). Save this session's key content:\n"
    "1. mempalace_diary_write — AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — verbatim quotes, decisions, code snippets\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Continue conversation after saving."
)

PRECOMPACT_BLOCK_REASON = (
    "COMPACTION IMMINENT (MemPalace). Save ALL session content before context is lost:\n"
    "1. mempalace_diary_write — thorough AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — ALL verbatim quotes, decisions, code, context\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Be thorough — after compaction, detailed context will be lost. "
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Save everything to MemPalace, then allow compaction to proceed."
)


def _mine_approval_reason(mine_dir: str, daemon_url: str) -> str:
    return (
        f"AUTO-INGEST requested (MemPalace).\n"
        f"Target directory: {mine_dir}\n\n"
        f"Show the user this directory and ask them to approve or deny mining it into the palace.\n"
        f"  Approve → POST {{\"dir\": \"{mine_dir}\", \"mode\": \"auto\"}} to {daemon_url}/mine\n"
        f"  Deny    → inform user, continue."
    )


def _sanitize_session_id(session_id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


def _validate_transcript_path(transcript_path: str) -> Path:
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser().resolve()
    if path.suffix not in (".jsonl", ".json"):
        return None
    if ".." in Path(transcript_path).parts:
        return None
    return path


_state_dir_initialized = False


def _log(message: str):
    global _state_dir_initialized
    try:
        if not _state_dir_initialized:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                STATE_DIR.chmod(0o700)
            except (OSError, NotImplementedError):
                pass
            _state_dir_initialized = True
        log_path = STATE_DIR / "hook.log"
        is_new = not log_path.exists()
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
        if is_new:
            try:
                log_path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
    except OSError:
        pass


def _output(data: dict):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _read_last_save_ts(session_id: str) -> float:
    ts_file = STATE_DIR / f"{session_id}_last_save_ts"
    try:
        return float(ts_file.read_text().strip())
    except Exception:
        return 0.0


def _write_last_save_ts(session_id: str):
    ts_file = STATE_DIR / f"{session_id}_last_save_ts"
    try:
        ts_file.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _load_hook_settings() -> dict:
    try:
        return json.loads(HOOK_SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _get_transcript_state(transcript_path: str) -> dict:
    import hashlib
    path = _validate_transcript_path(transcript_path)
    if path is None or not path.is_file():
        return {"count": 0, "turns": []}

    path_hash = hashlib.md5(str(path).encode('utf-8')).hexdigest()

    global _state_dir_initialized
    if not _state_dir_initialized:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            STATE_DIR.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        _state_dir_initialized = True

    cache_path = STATE_DIR / f"{path_hash}_parse_cache.json"

    state = {"size": 0, "count": 0, "turns": []}
    try:
        if cache_path.is_file():
            with open(cache_path, "r", encoding="utf-8") as f:
                state = json.load(f)
    except Exception:
        pass

    try:
        stat = path.stat()
        current_size = stat.st_size
    except OSError:
        return {"count": 0, "turns": []}

    if current_size < state.get("size", 0):
        # file truncated or rotated
        state = {"size": 0, "count": 0, "turns": []}
    elif current_size == state.get("size", 0):
        return state

    new_count = state.get("count", 0)
    new_turns = state.get("turns", [])

    try:
        with open(path, "rb") as f:
            if state.get("size", 0) > 0:
                f.seek(state.get("size", 0))

            while True:
                line_bytes = f.readline()
                if not line_bytes:
                    break
                if not line_bytes.endswith(b'\n'):
                    # incomplete line, skip parsing and stop here
                    break

                state["size"] = f.tell()

                line = line_bytes.decode('utf-8', errors='replace')
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                except (json.JSONDecodeError, AttributeError):
                    continue

                # --- Count logic ---
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    is_counted = True
                    if isinstance(content, str):
                        if "<command-message>" in content:
                            is_counted = False
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict)
                        )
                        if "<command-message>" in text:
                            is_counted = False
                        else:
                            text_blocks = [
                                b for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            if not text_blocks:
                                is_counted = False
                    if is_counted:
                        new_count += 1
                elif entry.get("type") == "event_msg":
                    payload = entry.get("payload", {})
                    if isinstance(payload, dict) and payload.get("type") == "user_message":
                        msg_text = payload.get("message", "")
                        if isinstance(msg_text, str) and "<command-message>" not in msg_text:
                            new_count += 1

                # --- Extract logic ---
                if isinstance(msg, dict):
                    role = msg.get("role")
                    if role in ("user", "assistant"):
                        content = msg.get("content", "")
                        text = ""
                        if isinstance(content, str):
                            text = content.strip()
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "").strip()
                                for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ).strip()
                        if text and "<command-message>" not in text:
                            new_turns.append({"role": role, "text": text[:600]})
                            if len(new_turns) > 200:
                                new_turns = new_turns[-200:]

    except OSError:
        pass

    state["count"] = new_count
    state["turns"] = new_turns

    try:
        tmp_cache_path = cache_path.with_suffix(".tmp")
        with open(tmp_cache_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        tmp_cache_path.replace(cache_path)
    except OSError:
        pass

    return state


def _count_human_messages(transcript_path: str) -> int:
    state = _get_transcript_state(transcript_path)
    return state.get("count", 0)


def _extract_messages(transcript_path: str, max_turns: int = 30) -> list:
    """Extract last N user+assistant text turns from the session JSONL."""
    state = _get_transcript_state(transcript_path)
    turns = state.get("turns", [])
    if max_turns and len(turns) > max_turns:
        return turns[-max_turns:]
    return turns


def _auth_headers() -> dict:
    """Return X-Api-Key header if PALACE_API_KEY is set, else empty dict."""
    key = os.environ.get("PALACE_API_KEY", "")
    if key:
        return {"X-Api-Key": key}
    return {}


def _post_digest(daemon_url: str, session_id: str, agent_name: str,
                 harness: str, messages: list, exchange_count: int) -> bool:
    payload = {
        "session_id": session_id,
        "agent_name": agent_name,
        "harness": harness,
        "messages": messages,
        "exchange_count": exchange_count,
        "topic": CHECKPOINT_TOPIC,
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/digest",
            data=data,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 202)
    except Exception:
        return False


def _get_mine_dir() -> str:
    """Return mine directory from MEMPAL_DIR only. No transcript path fallback."""
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        return mempal_dir
    return ""


def _post_mcp(daemon_url: str, tool_name: str, params: dict) -> bool:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/mcp",
            data=data,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _post_mine(daemon_url: str, mine_dir: str, timeout: int = 60) -> bool:
    """POST /mine to daemon. No subprocess fallback — if daemon down, returns False."""
    payload = json.dumps({"dir": mine_dir, "mode": "auto"}).encode()
    try:
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/mine",
            data=payload,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception as e:
        _log(f"mine via daemon failed (daemon unreachable): {e}")
        return False


def _desktop_notify(title: str, body: str) -> None:
    try:
        subprocess.Popen(
            ["notify-send", "--expire-time=4000", "--icon=dialog-information", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError):
        pass


def _detach() -> bool:
    """Fork into a background process that survives harness pipe closure.

    Returns True in the child (caller should continue with slow work),
    False in the parent (caller should exit promptly).

    The child becomes a session leader and redirects std streams to /dev/null
    so writes to the now-closed harness pipes don't cause SIGPIPE.

    If fork fails (shouldn't happen on Linux), returns True so the caller
    falls back to synchronous execution — the work still happens, just with
    the risk of being killed by the harness.
    """
    try:
        pid = os.fork()
    except OSError as e:
        _log(f"fork failed, falling back to synchronous: {e}")
        return True  # fall back: caller continues synchronously

    if pid > 0:
        # Parent — exit immediately so the harness can close its pipes
        return False

    # Child — detach fully
    try:
        os.setsid()
    except OSError:
        pass

    # Redirect stdin/stdout/stderr to /dev/null so writes don't SIGPIPE
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    finally:
        if devnull > 2:
            os.close(devnull)

    return True


def _parse_harness_input(data: dict, harness: str) -> dict:
    if harness not in SUPPORTED_HARNESSES:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(1)
    return {
        "session_id": _sanitize_session_id(str(data.get("session_id", "unknown"))),
        "stop_hook_active": data.get("stop_hook_active", False),
        "transcript_path": str(data.get("transcript_path", "")),
    }


def _prune_state_files(max_age_days: int = 7):
    cutoff = time.time() - max_age_days * 86400
    try:
        for f in STATE_DIR.iterdir():
            if f.name in ("hook.log",):
                continue
            if f.suffix in ("", ) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def hook_session_start(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    _log(f"SESSION START for session {session_id}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_last_save_ts(session_id)   # seed so first-stop time_trigger doesn't fire immediately
    _prune_state_files()
    _output({})


def hook_stop(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    stop_hook_active = parsed["stop_hook_active"]
    transcript_path = parsed["transcript_path"]

    if str(stop_hook_active).lower() in ("true", "1", "yes"):
        _output({})
        return

    exchange_count = _count_human_messages(transcript_path)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_save_file = STATE_DIR / f"{session_id}_last_save"
    last_save = 0
    if last_save_file.is_file():
        try:
            last_save = int(last_save_file.read_text().strip())
        except (ValueError, OSError):
            last_save = 0

    if exchange_count < last_save:
        _log(
            f"Session {session_id}: rebased stale last_save "
            f"({last_save} → {exchange_count}) — counter went backward, "
            f"likely after a count-rule change"
        )
        last_save = exchange_count

    since_last = exchange_count - last_save
    last_save_ts = _read_last_save_ts(session_id)
    time_since_last = time.time() - last_save_ts

    _log(f"Session {session_id}: {exchange_count} exchanges, {since_last} since last save, {time_since_last:.0f}s elapsed")

    settings = _load_hook_settings()

    # Three independent triggers — any one fires a save:
    #   count   — every SAVE_INTERVAL exchanges (existing behaviour)
    #   time    — every TIME_SAVE_INTERVAL seconds with unsaved exchanges (new)
    #   force   — force_on_stop=true: save at session end even with few exchanges,
    #             subject to FORCE_MIN_INTERVAL to prevent per-response spam
    count_trigger = since_last >= SAVE_INTERVAL and exchange_count > 0
    time_trigger  = time_since_last >= TIME_SAVE_INTERVAL and since_last > 0
    force_trigger = (
        settings.get("force_on_stop", True)
        and since_last > 0
        and time_since_last >= settings.get("force_min_interval", FORCE_MIN_INTERVAL)
    )

    if not (count_trigger or time_trigger or force_trigger):
        _output({})
        return

    trigger = "count" if count_trigger else ("time" if time_trigger else "force")
    _log(f"TRIGGERING SAVE at exchange {exchange_count} (trigger={trigger})")

    try:
        last_save_file.write_text(str(exchange_count), encoding="utf-8")
    except OSError:
        pass
    _write_last_save_ts(session_id)

    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    silent = settings.get("silent_save", True)
    toast = settings.get("desktop_toast", False)

    mine_dir = _get_mine_dir()
    if mine_dir:
        _log(f"Mine approval requested for {mine_dir}")
        if toast:
            _desktop_notify("MemPalace", f"Mine approval needed: {mine_dir}")
        _output({"decision": "block", "reason": _mine_approval_reason(mine_dir, daemon_url)})
        return

    if silent:
        # Output to harness BEFORE forking — harness gets valid JSON immediately
        _output({})

        # Detach so diary save survives harness pipe closure
        if not _detach():
            return  # parent — already printed output, exit promptly

        # Child continues here — stdout is /dev/null, work silently
        msgs = _extract_messages(transcript_path, max_turns=40)
        date_str = datetime.now().strftime("%Y-%m-%d")
        if msgs:
            user_turns = [m["text"][:200] for m in msgs if m["role"] == "user"]
            bullets = "\n".join(f"- {t}" for t in user_turns[-10:])
            entry = f"SESSION:{date_str}|{harness}+{exchange_count}msgs|★★★☆☆\n\n{bullets}"
        else:
            entry = f"SESSION:{date_str}|{harness}+{exchange_count}msgs|★★☆☆☆\n\n- (transcript unavailable)"
        ok = _post_mcp(daemon_url, "mempalace_diary_write", {
            "agent_name": harness,
            "entry": entry,
            "topic": CHECKPOINT_TOPIC,
        })
        _log(f"Silent save {'OK' if ok else 'FAILED (daemon unreachable)'} at exchange {exchange_count}")
        if toast:
            _desktop_notify("MemPalace", f"Auto-saved at {exchange_count} msgs ({trigger})")
    else:
        if toast:
            _desktop_notify("MemPalace checkpoint", "Save requested — check Claude")
        _output({"decision": "block", "reason": STOP_BLOCK_REASON})


def hook_precompact(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    _log(f"PRE-COMPACT triggered for session {session_id}")

    settings = _load_hook_settings()
    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    toast = settings.get("desktop_toast", False)

    mine_dir = _get_mine_dir()
    if not mine_dir:
        _log("Precompact mine skipped: MEMPAL_DIR not set")
        _output({})
        return

    # Output to harness BEFORE forking — harness gets valid JSON immediately
    _output({})

    # Detach so mine operation survives harness pipe closure
    if not _detach():
        return  # parent — already printed output, exit promptly

    # Child continues here — stdout is /dev/null, work silently
    _log(f"Precompact mine via daemon: {mine_dir}")
    ok = _post_mine(daemon_url, mine_dir, timeout=60)
    _log(f"Precompact mine {'OK' if ok else 'skipped (daemon unreachable)'}")

    if toast:
        _desktop_notify("MemPalace", "Pre-compaction checkpoint triggered")


def run_hook(hook_name: str, harness: str):
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _log("WARNING: Failed to parse stdin JSON, proceeding with empty data")
        data = {}

    hooks = {
        "session-start": hook_session_start,
        "stop": hook_stop,
        "precompact": hook_precompact,
    }

    handler = hooks.get(hook_name)
    if handler is None:
        print(f"Unknown hook: {hook_name}", file=sys.stderr)
        sys.exit(1)

    handler(data, harness)


def main():
    parser = argparse.ArgumentParser(description="palace-daemon hook runner")
    parser.add_argument("--hook", required=True, choices=["session-start", "stop", "precompact"])
    parser.add_argument("--harness", required=True)
    args = parser.parse_args()
    run_hook(args.hook, args.harness)


if __name__ == "__main__":
    main()
