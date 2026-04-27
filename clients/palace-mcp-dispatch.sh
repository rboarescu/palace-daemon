#!/bin/bash
# Dispatches MCP server based on PALACE_DAEMON_URL env var.
#   set    → proxy to daemon (mempalace-mcp.py)
#   unset  → in-process mempalace.mcp_server (local palace)
#
# Resolve the sibling mempalace-mcp.py relative to this script. Uses
# `cd "$(dirname …)" && pwd -P` rather than `readlink -f` because
# readlink's `-f` flag is GNU-specific (BSD readlink on macOS doesn't
# accept it). `pwd -P` resolves symlinks in the directory path via
# the filesystem, which covers the actual common case for plugin
# invocations (the script's parent directory may be a symlink, e.g.
# under a Claude Code plugin cache); the dispatcher itself is rarely
# a symlink.

PYTHON="${MEMPALACE_PYTHON:-python3}"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd -P)"
MCP_CLIENT="$HERE/mempalace-mcp.py"

if [ -n "$PALACE_DAEMON_URL" ]; then
  if [ ! -f "$MCP_CLIENT" ]; then
    echo "palace-mcp-dispatch: missing sibling client at $MCP_CLIENT" >&2
    echo "  expected mempalace-mcp.py to live next to this script." >&2
    exit 1
  fi
  exec "$PYTHON" "$MCP_CLIENT" --daemon "$PALACE_DAEMON_URL"
else
  exec "$PYTHON" -m mempalace.mcp_server
fi
