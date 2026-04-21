#!/usr/bin/env python3
"""
mempalace-mcp — stdio MCP proxy for palace-daemon

Bridges any MCP client (Claude Code, Claude Desktop, etc.) to a remote
palace-daemon instance over HTTP. Reads JSON-RPC from stdin, forwards to
the daemon, writes responses to stdout.

Usage:
    python mempalace-mcp.py --daemon http://192.168.0.42:8085
    PALACE_DAEMON_URL=http://192.168.0.42:8085 python mempalace-mcp.py

Claude Code setup (~/.claude/settings.json):
    {
      "mcpServers": {
        "mempalace": {
          "command": "python",
          "args": ["/path/to/mempalace-mcp.py", "--daemon", "http://YOUR_SERVER:8085"]
        }
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
        health_url = url.rstrip("/") + "/health"
        req = urllib.request.urlopen(health_url, timeout=3)
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


def main():
    parser = argparse.ArgumentParser(description="MCP stdio proxy for palace-daemon")
    parser.add_argument("--daemon", default=DEFAULT_DAEMON, help="palace-daemon base URL")
    parser.add_argument("--api-key", default=API_KEY, help="API key (or set PALACE_API_KEY)")
    args = parser.parse_args()

    global API_KEY
    if args.api_key:
        API_KEY = args.api_key

    if not find_daemon(args.daemon):
        print(f"palace-daemon unreachable at {args.daemon}", file=sys.stderr)
        sys.exit(1)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = forward(args.daemon, request)
        except urllib.error.URLError as e:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32000, "message": f"Daemon unreachable: {e}"},
            }
        except Exception as e:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32000, "message": str(e)},
            }
        if request.get("id") is None:
            continue
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
