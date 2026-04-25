#!/usr/bin/env bash
# palace-daemon client bootstrap
# Sets up mempalace-mcp.py + hook.py on a client machine and wires them
# into the requested AI tools, pointing at a remote palace-daemon.
#
# Usage:
#   bash bootstrap.sh --daemon http://10.0.0.5:8085 --tool claude-code
#   bash bootstrap.sh --daemon http://10.0.0.5:8085 --tool all
#
# --tool options: claude-code | gemini | vscode | cursor | jetbrains | all
#
# Files installed to: ~/.local/share/mempalace/
# Settings written to: ~/.mempalace/hook_settings.json

set -euo pipefail

DAEMON_URL=""
TOOL=""
# Default host and path (can be overridden via ENV)
ARTEMIS_HOST="${ARTEMIS_HOST:-user@daemon-host}"
ARTEMIS_CLIENTS_PATH="${ARTEMIS_CLIENTS_PATH:-/path/to/palace-daemon/clients}"
INSTALL_DIR="$HOME/.local/share/mempalace"
HOOK_SETTINGS="$HOME/.mempalace/hook_settings.json"

usage() {
    echo "Usage: $0 --daemon <url> --tool <tool>"
    echo "  --daemon  palace-daemon URL (e.g. http://10.0.0.5:8085)"
    echo "  --tool    one of: claude-code | gemini | vscode | cursor | jetbrains | all"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --daemon) DAEMON_URL="$2"; shift 2 ;;
        --tool)   TOOL="$2";       shift 2 ;;
        *) usage ;;
    esac
done

[[ -z "$DAEMON_URL" || -z "$TOOL" ]] && usage

# ── 1. Install client files ───────────────────────────────────────────────────

mkdir -p "$INSTALL_DIR"

echo "→ Copying mempalace-mcp.py and hook.py from Artemis..."
scp "$ARTEMIS_HOST:$ARTEMIS_CLIENTS_PATH/mempalace-mcp.py" "$INSTALL_DIR/mempalace-mcp.py" \
    || { echo "ERROR: scp failed for mempalace-mcp.py — check ARTEMIS_HOST ($ARTEMIS_HOST) and connectivity"; exit 1; }
scp "$ARTEMIS_HOST:$ARTEMIS_CLIENTS_PATH/hook.py"          "$INSTALL_DIR/hook.py" \
    || { echo "ERROR: scp failed for hook.py — check ARTEMIS_HOST ($ARTEMIS_HOST) and connectivity"; exit 1; }
chmod +x "$INSTALL_DIR/mempalace-mcp.py" "$INSTALL_DIR/hook.py"
echo "   Installed to $INSTALL_DIR"

# ── 2. Write hook_settings.json ───────────────────────────────────────────────

mkdir -p "$HOME/.mempalace"
cat > "$HOOK_SETTINGS" <<EOF
{
  "silent_save": true,
  "desktop_toast": false,
  "daemon_url": "$DAEMON_URL"
}
EOF
echo "→ Written $HOOK_SETTINGS (daemon_url=$DAEMON_URL)"

# ── 3. Tool-specific config ───────────────────────────────────────────────────

MCP_CMD="python3"
MCP_ARGS="[\"$INSTALL_DIR/mempalace-mcp.py\", \"--daemon\", \"$DAEMON_URL\"]"
HOOK_CMD="python3 $INSTALL_DIR/hook.py"

# ── claude-code ───────────────────────────────────────────────────────────────
setup_claude_code() {
    echo "→ Configuring Claude Code..."

    CLAUDE_JSON="$HOME/.claude.json"
    if [[ ! -f "$CLAUDE_JSON" ]]; then
        echo "{}" > "$CLAUDE_JSON"
    fi

    # Inject mcpServers block using python3 (avoids jq dependency)
    python3 - "$CLAUDE_JSON" "$INSTALL_DIR/mempalace-mcp.py" "$DAEMON_URL" <<'PYEOF'
import json, sys
path, mcp_py, daemon = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    d = json.load(f)
d.setdefault("mcpServers", {})["mempalace"] = {
    "type": "stdio",
    "command": "python3",
    "args": [mcp_py, "--daemon", daemon],
    "env": {}
}
with open(path, "w") as f:
    json.dump(d, f, indent=2)
print(f"   Updated {path}")
PYEOF

    CLAUDE_SETTINGS="$HOME/.claude/settings.json"
    mkdir -p "$HOME/.claude"
    if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
        echo "{}" > "$CLAUDE_SETTINGS"
    fi

    python3 - "$CLAUDE_SETTINGS" "$INSTALL_DIR/hook.py" <<'PYEOF'
import json, sys
path, hook_py = sys.argv[1], sys.argv[2]
with open(path) as f:
    d = json.load(f)
d["hooks"] = {
    "Stop": [{"hooks": [{"type": "command",
        "command": f"python3 {hook_py} --hook stop --harness claude-code",
        "timeout": 30}]}],
    "PreCompact": [{"hooks": [{"type": "command",
        "command": f"python3 {hook_py} --hook precompact --harness claude-code",
        "timeout": 60}]}]
}
with open(path, "w") as f:
    json.dump(d, f, indent=2)
print(f"   Updated {path}")
PYEOF
}

# ── gemini ────────────────────────────────────────────────────────────────────
setup_gemini() {
    echo "→ Configuring Gemini CLI..."

    GEMINI_SETTINGS="$HOME/.gemini/settings.json"
    mkdir -p "$HOME/.gemini"
    if [[ ! -f "$GEMINI_SETTINGS" ]]; then
        echo "{}" > "$GEMINI_SETTINGS"
    fi

    python3 - "$GEMINI_SETTINGS" "$INSTALL_DIR/mempalace-mcp.py" "$DAEMON_URL" "$INSTALL_DIR/hook.py" <<'PYEOF'
import json, sys
path, mcp_py, daemon, hook_py = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(path) as f:
    d = json.load(f)
d.setdefault("mcpServers", {})["mempalace"] = {
    "command": "python3",
    "args": ["--", mcp_py, "--daemon", daemon],
    "trust": True
}
d["hooks"] = {
    "SessionStart": [{"name": "mempalace-session-start", "type": "command",
        "command": "python3", "args": [hook_py, "--hook", "session-start", "--harness", "gemini-cli"]}],
    "SessionEnd": [{"name": "mempalace-session-stop", "type": "command",
        "command": "python3", "args": [hook_py, "--hook", "stop", "--harness", "gemini-cli"]}],
    "PreCompress": [{"name": "mempalace-precompact", "type": "command",
        "command": "python3", "args": [hook_py, "--hook", "precompact", "--harness", "gemini-cli"],
        "timeout": 30}]
}
with open(path, "w") as f:
    json.dump(d, f, indent=2)
print(f"   Updated {path}")
PYEOF
}

# ── vscode ────────────────────────────────────────────────────────────────────
setup_vscode() {
    echo "→ Configuring VSCode (user-level MCP)..."
    # VSCode reads ~/.vscode/mcp.json for user-level MCP servers (Cline, Copilot, etc.)
    VSCODE_MCP="$HOME/.vscode/mcp.json"
    mkdir -p "$HOME/.vscode"

    python3 - "$VSCODE_MCP" "$INSTALL_DIR/mempalace-mcp.py" "$DAEMON_URL" <<'PYEOF'
import json, sys
from pathlib import Path
path, mcp_py, daemon = sys.argv[1], sys.argv[2], sys.argv[3]
p = Path(path)
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault("servers", {})["mempalace"] = {
    "type": "stdio",
    "command": "python3",
    "args": [mcp_py, "--daemon", daemon]
}
p.write_text(json.dumps(d, indent=2))
print(f"   Updated {path}")
PYEOF
}

# ── cursor ────────────────────────────────────────────────────────────────────
setup_cursor() {
    echo "→ Configuring Cursor..."
    CURSOR_MCP="$HOME/.cursor/mcp.json"
    mkdir -p "$HOME/.cursor"

    python3 - "$CURSOR_MCP" "$INSTALL_DIR/mempalace-mcp.py" "$DAEMON_URL" <<'PYEOF'
import json, sys
from pathlib import Path
path, mcp_py, daemon = sys.argv[1], sys.argv[2], sys.argv[3]
p = Path(path)
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault("mcpServers", {})["mempalace"] = {
    "command": "python3",
    "args": [mcp_py, "--daemon", daemon]
}
p.write_text(json.dumps(d, indent=2))
print(f"   Updated {path}")
PYEOF
}

# ── jetbrains (Android Studio, IDEA, etc.) ────────────────────────────────────
setup_jetbrains() {
    echo "→ Configuring JetBrains (MCP Host plugin required)..."
    # Detect config root
    if [[ "$(uname)" == "Darwin" ]]; then
        JB_BASE="$HOME/Library/Application Support/JetBrains"
    else
        JB_BASE="$HOME/.config/JetBrains"
    fi

    if [[ ! -d "$JB_BASE" ]]; then
        echo "   No JetBrains config dir found at $JB_BASE — skipping"
        echo "   Install the 'MCP Host' plugin in your JetBrains IDE first."
        return
    fi

    # Apply to all found IDE dirs
    for ide_dir in "$JB_BASE"/*/; do
        MCP_FILE="$ide_dir/mcp.json"
        python3 - "$MCP_FILE" "$INSTALL_DIR/mempalace-mcp.py" "$DAEMON_URL" <<'PYEOF'
import json, sys
from pathlib import Path
path, mcp_py, daemon = sys.argv[1], sys.argv[2], sys.argv[3]
p = Path(path)
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault("servers", {})["mempalace"] = {
    "command": "python3",
    "args": [mcp_py, "--daemon", daemon]
}
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2))
print(f"   Updated {path}")
PYEOF
    done
}

# ── dispatch ──────────────────────────────────────────────────────────────────

case "$TOOL" in
    claude-code) setup_claude_code ;;
    gemini)      setup_gemini ;;
    vscode)      setup_vscode ;;
    cursor)      setup_cursor ;;
    jetbrains)   setup_jetbrains ;;
    all)
        setup_claude_code
        setup_gemini
        setup_vscode
        setup_cursor
        setup_jetbrains
        ;;
    *) echo "Unknown tool: $TOOL"; usage ;;
esac

echo ""
echo "✓ Done. Verify with: curl $DAEMON_URL/health"
