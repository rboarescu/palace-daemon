#!/usr/bin/env python3
with open("/tmp/mcp_debug.log", "a") as f: f.write("SCRIPT STARTED\n")
"""
mempalace-mcp — stdio MCP proxy for palace-daemon, with direct fallback

Primary mode: bridges MCP client → palace-daemon over HTTP (serialized,
semaphore-protected, all clients coordinated through one chokepoint).

Fallback mode: if the daemon is unreachable at startup, imports
mempalace.mcp_server directly and handles requests in-process (same as
the plain direct setup, minus the daemon's concurrency guarantees).

Usage:
    python mempalace-mcp.py --daemon http://192.168.0.42:8085
    PALACE_DAEMON_URL=http://192.168.0.42:8085 python mempalace-mcp.py

Claude Code setup (~/.claude.json mcpServers):
    {
      "mempalace": {
        "type": "stdio",
        "command": "/path/to/venv/python",
        "args": ["/path/to/mempalace-mcp.py", "--daemon", "http://localhost:8085"]
      }
    }
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_DAEMON = os.getenv("PALACE_DAEMON_URL", "http://localhost:8085")
API_KEY = os.getenv("PALACE_API_KEY", "")


def find_daemon(url: str) -> bool:
    try:
        req = urllib.request.urlopen(url.rstrip("/") + "/health", timeout=3)
        return req.status == 200
    except Exception:
        return False


def forward(url: str, request: dict) -> dict:
    data = json.dumps(request).encode()
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY
    req = urllib.request.Request(
        url.rstrip("/") + "/mcp",
        data=data,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _stdio_loop(handle_line):
    """Read JSON-RPC lines from stdin, call handle_line, print responses."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_line(request)
        if response is not None and request.get("id") is not None:
            print(json.dumps(response), flush=True)


def run_daemon_mode(daemon_url: str):
    def handle(request):
        try:
            return forward(daemon_url, request)
        except urllib.error.URLError as e:
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32000, "message": f"Daemon unreachable: {e}"}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32000, "message": str(e)}}

    _stdio_loop(handle)


def main():
    parser = argparse.ArgumentParser(description="MCP stdio proxy for palace-daemon")
    parser.add_argument("--daemon", default=DEFAULT_DAEMON, help="palace-daemon base URL")
    parser.add_argument("--api-key", default=None, help="API key (or set PALACE_API_KEY)")
    args = parser.parse_args()

    global API_KEY
    if args.api_key is not None:
        API_KEY = args.api_key

    if find_daemon(args.daemon):
        print(f"palace-daemon: connected at {args.daemon}", file=open("/tmp/mcp_debug.log", "a"))
        run_daemon_mode(args.daemon)
    else:
        print(f"ERROR: palace-daemon unreachable at {args.daemon}. Direct fallback disabled for safety.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
