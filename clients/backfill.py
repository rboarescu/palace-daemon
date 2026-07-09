#!/usr/bin/env python3
"""
MemPalace session backfill — converts existing Claude Code JSONL transcripts
into AAAK diary entries in the palace.

Usage:
    python3 backfill.py [--projects-dir DIR] [--dry-run] [--min-turns N]

Options:
    --projects-dir DIR   Root of Claude Code projects (default: ~/.claude/projects)
    --dry-run            Print entries without writing to palace
    --min-turns N        Skip sessions with fewer than N user turns (default: 3)
    --harness NAME       Agent name for diary entries (default: claude-code)
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOOK_SETTINGS = Path.home() / ".mempalace" / "hook_settings.json"
CHECKPOINT_TOPIC = "checkpoint"

class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def consume(self, tokens: float = 1.0):
        while True:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return

            # Not enough tokens, wait until enough accumulate
            wait_time = (tokens - self.tokens) / self.rate
            time.sleep(wait_time)
def load_daemon_url() -> str:
    try:
        s = json.loads(HOOK_SETTINGS.read_text())
        return s.get("daemon_url", "http://localhost:8085")
    except Exception:
        return "http://localhost:8085"


def post_mcp(daemon_url: str, tool_name: str, params: dict) -> bool:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }).encode()
    try:
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/mcp",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  [!] daemon error: {e}", file=sys.stderr)
        return False


def extract_messages(jsonl_path: str, max_turns: int = 40) -> tuple[list, str]:
    """Returns (turns, session_date_str). turns = [{"role": ..., "text": ...}]"""
    turns = []
    session_date = ""
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    # Grab date from first timestamp seen
                    if not session_date and entry.get("timestamp"):
                        ts = entry["timestamp"]
                        session_date = ts[:10]  # "YYYY-MM-DD"

                    msg = entry.get("message", {})
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "").strip()
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()
                    else:
                        continue
                    if not text or "<command-message>" in text:
                        continue
                    turns.append({"role": role, "text": text[:600]})
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        pass
    return turns[-max_turns:], session_date or datetime.now().strftime("%Y-%m-%d")


def project_label(jsonl_path: str) -> str:
    """Derive a short project name from the directory path."""
    parts = Path(jsonl_path).parent.name  # e.g. "-mnt-ai-storage-Projects-mneme"
    # Strip leading dash, replace remaining dashes with slashes, take last 2 segments
    clean = parts.lstrip("-").replace("-", "/")
    segments = [s for s in clean.split("/") if s]
    return "/".join(segments[-2:]) if len(segments) >= 2 else clean


def make_aaak(date_str: str, project: str, harness: str, turns: list, n_total: int) -> str:
    user_turns = [t["text"][:200] for t in turns if t["role"] == "user"]
    bullets = "\n".join(f"- {t}" for t in user_turns[-10:])
    return f"SESSION:{date_str}|{project}+{n_total}msgs|★★★☆☆\n\n{bullets}"


def main():
    parser = argparse.ArgumentParser(description="Backfill Claude Code sessions into MemPalace diary")
    parser.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-turns", type=int, default=3)
    parser.add_argument("--harness", default="claude-code")
    args = parser.parse_args()

    daemon_url = load_daemon_url()
    print(f"Daemon: {daemon_url}")
    print(f"Projects dir: {args.projects_dir}")
    print(f"Dry run: {args.dry_run}\n")

    # Check daemon reachable
    if not args.dry_run:
        try:
            with urllib.request.urlopen(f"{daemon_url.rstrip('/')}/health", timeout=5) as r:
                health = json.loads(r.read())
                print(f"Daemon healthy: {health.get('version', '?')}\n")
        except Exception as e:
            print(f"[!] Daemon unreachable: {e}")
            sys.exit(1)

    jsonl_files = sorted(glob.glob(f"{args.projects_dir}/**/*.jsonl", recursive=True))
    print(f"Found {len(jsonl_files)} session files\n")

    written = skipped_short = skipped_empty = 0
    # Rate limit to 10 requests per second with a burst capacity of 10
    rate_limiter = TokenBucket(rate=10.0, capacity=10.0)

    for i, path in enumerate(jsonl_files, 1):
        turns, date_str = extract_messages(path)
        user_turns = [t for t in turns if t["role"] == "user"]
        n_user = len(user_turns)

        session_id = Path(path).stem
        project = project_label(path)
        prefix = f"[{i:3d}/{len(jsonl_files)}] {date_str} {session_id[:8]}…"

        if not turns:
            print(f"{prefix} SKIP (no readable messages)")
            skipped_empty += 1
            continue

        if n_user < args.min_turns:
            print(f"{prefix} SKIP ({n_user} user turns < {args.min_turns})")
            skipped_short += 1
            continue

        # Count total messages for label (re-read quickly)
        total_user = 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        m = e.get("message", {})
                        if isinstance(m, dict) and m.get("role") == "user":
                            c = m.get("content", "")
                            txt = c if isinstance(c, str) else " ".join(
                                b.get("text", "") for b in c if isinstance(b, dict)
                            )
                            if "<command-message>" not in txt:
                                total_user += 1
                    except Exception:
                        pass
        except OSError:
            total_user = n_user

        entry = make_aaak(date_str, project, args.harness, turns, total_user)

        if args.dry_run:
            print(f"{prefix} DRY-RUN ({n_user} turns)")
            print(f"  {entry[:120].replace(chr(10), ' | ')}")
        else:
            ok = post_mcp(daemon_url, "mempalace_diary_write", {
                "agent_name": args.harness,
                "entry": entry,
                "topic": CHECKPOINT_TOPIC,
            })
            status = "OK" if ok else "FAIL"
            print(f"{prefix} {status} ({n_user} turns)")
            written += 1
            rate_limiter.consume()

    print(f"\nDone. written={written} skipped_short={skipped_short} skipped_empty={skipped_empty}")


if __name__ == "__main__":
    main()
