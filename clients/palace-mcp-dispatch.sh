#!/bin/bash
# Dispatches MCP server based on PALACE_DAEMON_URL env var.
#   set    → proxy to daemon (mempalace-mcp.py)
#   unset  → in-process mempalace.mcp_server (local palace)

PYTHON="${MEMPALACE_PYTHON:-python3}"

if [ -n "$PALACE_DAEMON_URL" ]; then
  exec "$PYTHON" /home/jp/Projects/palace-daemon/clients/mempalace-mcp.py --daemon "$PALACE_DAEMON_URL"
else
  exec "$PYTHON" -m mempalace.mcp_server
fi
