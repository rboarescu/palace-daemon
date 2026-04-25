#!/usr/bin/env python3
# Stdlib-only stop hook — bypasses mempalace import to avoid chromadb HNSW
# cold-start segfaults. Requires PALACE_DAEMON_URL set.
import json, os, re, sys, urllib.request
from pathlib import Path

SAVE_INTERVAL = 15
STATE_DIR = Path.home() / ".mempalace" / "hook_state"


def log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "hook.log").open("a") as f:
            from datetime import datetime
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def count_human_messages(transcript_path):
    if not transcript_path:
        return 0
    p = Path(transcript_path).expanduser()
    if not p.is_file() or p.suffix not in (".jsonl", ".json"):
        return 0
    n = 0
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    msg = e.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        c = msg.get("content", "")
                        text = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                        if "<command-message>" not in text:
                            n += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        pass
    return n


def wing_from_path(transcript_path):
    if not transcript_path:
        return "unknown"
    parts = Path(transcript_path).parts
    for i, part in enumerate(parts):
        if part == "projects" and i + 1 < len(parts):
            return re.sub(r"[^a-zA-Z0-9_-]", "_", parts[i + 1]) or "unknown"
    return "unknown"


def main():
    hook_type = sys.argv[1] if len(sys.argv) > 1 else "stop"
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}

    session_id = re.sub(r"[^a-zA-Z0-9_-]", "", data.get("session_id", "") or "") or "unknown"
    transcript_path = data.get("transcript_path", "")

    count = count_human_messages(transcript_path)
    last_save_file = STATE_DIR / f"{session_id}_last_save"
    try:
        last_save = int(last_save_file.read_text().strip()) if last_save_file.is_file() else 0
    except (ValueError, OSError):
        last_save = 0

    since_last = count - last_save
    log(f"Session {session_id}: {count} exchanges, {since_last} since last save (fast-path/{hook_type})")

    # Stop hooks gate on threshold; precompact always saves (compaction is imminent).
    if hook_type == "stop" and (since_last < SAVE_INTERVAL or count <= 0):
        print("{}")
        return
    if hook_type == "precompact" and count <= 0:
        print("{}")
        return

    log(f"TRIGGERING SAVE at exchange {count} (fast-path)")

    daemon_url = os.environ.get("PALACE_DAEMON_URL", "").strip().rstrip("/")
    if not daemon_url:
        log("ERROR: fast-path called without PALACE_DAEMON_URL")
        print("{}")
        return

    payload = {
        "session_id": session_id,
        "wing": wing_from_path(transcript_path),
        "entry": f"Stop checkpoint at {count} exchanges",
        "topic": "checkpoint",
        "agent_name": "session-hook",
        "themes": [],
        "message_count": since_last,
    }
    req = urllib.request.Request(
        f"{daemon_url}/silent-save",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        req.add_header("x-api-key", api_key)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        log(f"Daemon silent-save: queued={result.get('queued')} count={result.get('count')} (fast-path)")
        try:
            last_save_file.write_text(str(count))
        except OSError:
            pass
        print(json.dumps({"systemMessage": result.get("systemMessage", "")}))
    except Exception as e:
        log(f"Daemon silent-save failed (fast-path): {e}")
        print("{}")


if __name__ == "__main__":
    main()
