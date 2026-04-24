# Plan: Palace-daemon hook runner + network bootstrap

## Context
Stop/precompact hooks currently call `mempalace hook run`, which:
1. Spawns `mempalace mine` as a direct subprocess (bypasses daemon, causes split-brain)
2. Falls back to mining the Claude transcript dir when `MEMPAL_DIR` unset (rogue indexing)
3. Fires without user awareness or approval

**Constraint:** mempalace is a third-party package — never modify files in its pipx venv.
All fixes must live in `palace-daemon/` (user-maintained).

**Solution:** Create `palace-daemon/clients/hook.py` — a self-contained hook runner that fully replaces `mempalace hook run`. Switch all hook commands in client configs to point at it. It has zero mempalace dependency (stdlib only), survives mempalace upgrades.

---

## Part 0 — Save this plan to the daemon project

**First action after approval:**
Copy this plan to `palace-daemon/docs/hook-routing-fix.md` so it lives as permanent project context alongside the code.

---

## Part 1 — Create `palace-daemon/clients/hook.py`

Self-contained replacement for `mempalace hook run`. Uses only Python stdlib (no mempalace import).

### Responsibilities
- Read JSON from stdin (Claude Code / Gemini hook protocol)
- Count human exchanges in transcript (same logic as hooks_cli.py)
- Track last-save state in `~/.mempalace/hook_state/`
- On save interval trigger:
  - **Mine approval**: if mine dir is resolvable, return a `decision: block` showing the target dir and asking user to approve via Claude → Claude then calls `POST /mine` on approval
  - **Silent diary save**: if `silent_save: true` in hook_settings.json, POST to daemon's `/mcp` (mempalace_diary_write) and pass through
  - **Block for diary**: if `silent_save: false`, return block asking Claude to save diary
- Route all mine calls through `POST <daemon>/mine` — no subprocess, no fallback

### CLI interface (mirrors `mempalace hook run`)
```
python3 hook.py --hook stop --harness claude-code
python3 hook.py --hook precompact --harness claude-code
python3 hook.py --hook session-start --harness claude-code
```

### Key design decisions vs hooks_cli.py
| Behaviour | hooks_cli.py (old) | hook.py (new) |
|---|---|---|
| Mine execution | `subprocess Popen mempalace mine` | Block → user approves → Claude POSTs `/mine` |
| Mine fallback | transcript dir if `MEMPAL_DIR` unset | No mine if `MEMPAL_DIR` unset (explicit opt-in only) |
| Daemon down | Silent save fails, falls through | Passes through silently, no subprocess |
| Precompact mine | `subprocess run mempalace mine` | `POST <daemon>/mine`, skip if daemon down |
| mempalace dependency | Yes (runs in venv) | None (pure stdlib) |

### Mine approval block format
```
AUTO-INGEST requested (MemPalace).
Target directory: /path/to/dir

Approve or deny mining this directory into the palace.
  Approve → call: mempalace_mine tool, or POST {"dir": "/path/to/dir", "mode": "auto"} to <daemon>/mine
  Deny    → inform user, continue.
```

### Settings read
Reads `~/.mempalace/hook_settings.json`:
- `daemon_url` (default: `http://localhost:8085`)
- `silent_save` (default: `true`)
- `desktop_toast` (default: `false`)

---

## Part 2 — Update hook commands in client configs on Artemis

Switch from `mempalace hook run` to `palace-daemon/clients/hook.py`.

### `~/.claude/settings.json`
```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command",
      "command": "python3 /home/user/palace-daemon/clients/hook.py --hook stop --harness claude-code",
      "timeout": 30}]}],
    "PreCompact": [{"hooks": [{"type": "command",
      "command": "python3 /home/user/palace-daemon/clients/hook.py --hook precompact --harness claude-code",
      "timeout": 60}]}]
  }
}
```

### `~/.gemini/settings.json` hooks section
```json
"hooks": {
  "SessionStart": [{"name": "mempalace-session-start", "type": "command",
    "command": "python3",
    "args": ["/home/user/palace-daemon/clients/hook.py", "--hook", "session-start", "--harness", "codex"]}],
  "SessionEnd": [{"name": "mempalace-session-stop", "type": "command",
    "command": "python3",
    "args": ["/home/user/palace-daemon/clients/hook.py", "--hook", "stop", "--harness", "codex"]}],
  "PreCompact": [{"name": "mempalace-precompact", "type": "command",
    "command": "python3",
    "args": ["/home/user/palace-daemon/clients/hook.py", "--hook", "precompact", "--harness", "codex"],
    "timeout": 30}]
}
```

---

## Part 3 — Fix `~/.mempalace/hook_settings.json` on Artemis

```json
{
  "silent_save": true,
  "desktop_toast": false,
  "daemon_url": "http://localhost:8085"
}
```
(Currently has `10.0.0.5:8085` — normalise to localhost for the host machine.)

---

## Part 4 — Bootstrap script

**File:** `palace-daemon/clients/bootstrap.sh`

Runs on any client machine (Linux/macOS). Installs the MCP client and configures each tool to talk to Artemis.

### Usage
```bash
# scp from Artemis first, or fetch via curl if you add a /clients static route later
scp user@10.0.0.5:/home/user/palace-daemon/clients/bootstrap.sh ~/bootstrap.sh
bash bootstrap.sh --daemon http://10.0.0.5:8085 --tool claude-code
bash bootstrap.sh --daemon http://10.0.0.5:8085 --tool all
```

### What it does
1. Download `mempalace-mcp.py` from Artemis (via scp or HTTP) → `~/.local/share/mempalace/mempalace-mcp.py`
2. Download `hook.py` from Artemis → `~/.local/share/mempalace/hook.py`
   - **No mempalace install needed on client** — hook.py is stdlib-only
3. Write `~/.mempalace/hook_settings.json` with remote daemon URL
4. Patch per-tool config (see below)

### Tool configs written by bootstrap

| Tool | Config file | Has hooks? |
|---|---|---|
| claude-code | `~/.claude.json` (mcpServers) + `~/.claude/settings.json` (hooks) | Yes (Stop, PreCompact) |
| gemini | `~/.gemini/settings.json` | Yes (SessionStart, SessionEnd, PreCompact) |
| vscode | `~/.vscode/mcp.json` | No |
| cursor | `~/.cursor/mcp.json` | No |
| jetbrains | `~/.config/JetBrains/<IDE>/mcp.json` (Linux) or `~/Library/Application Support/JetBrains/<IDE>/mcp.json` (macOS) | No |

For tools with hooks: bootstrap writes hook commands pointing at the downloaded `hook.py`.
For MCP-only tools: bootstrap writes only the mcpServers block.

### Note on mempalace install on clients
`hook.py` is stdlib-only — **clients do not need to install mempalace**. The MCP server (`mempalace-mcp.py`) is also stdlib-only. So `pipx install mempalace` is only needed on Artemis (the host).

---

## Part 5 — Documentation updates (post-fix)

### `palace-daemon/README.md` — add/update sections:

1. **Clients section** — document `hook.py` as the recommended hook runner:
   - What it does, why it exists (replaces `mempalace hook run` to avoid direct DB access)
   - CLI usage for each harness
   - Link to per-tool config examples

2. **Hook settings** — document `~/.mempalace/hook_settings.json` fields (`daemon_url`, `silent_save`, `desktop_toast`)

3. **Bootstrap** — document `bootstrap.sh` usage and supported `--tool` values

4. **Remote clients** — expand the existing clients section with a table of supported tools and their config paths

---

## File summary

| File | Action |
|---|---|
| `palace-daemon/docs/hook-routing-fix.md` | **Create** — copy of this plan as permanent project context |
| `palace-daemon/clients/hook.py` | **Create** — stdlib-only hook runner replacing `mempalace hook run` |
| `palace-daemon/clients/bootstrap.sh` | **Create** — client setup script |
| `palace-daemon/README.md` | **Update** — document hook.py, bootstrap, remote client configs |
| `~/.claude/settings.json` | **Modify** — hook commands → `hook.py` |
| `~/.gemini/settings.json` | **Modify** — hook commands → `hook.py` |
| `~/.mempalace/hook_settings.json` | **Modify** — `daemon_url` → `localhost` |

---

## Verification

### Hook fix
1. Start daemon: `systemctl --user start palace-daemon`
2. Trigger stop hook (hit exchange multiple of 15); confirm approval block appears with mine dir
3. Approve: confirm `journalctl --user -u palace-daemon` shows POST /mine; no subprocess spawned
4. Deny: no mine process, conversation continues
5. Stop daemon: hook fires, passes through silently

### Bootstrap
1. Run on a second LAN machine: `bash bootstrap.sh --daemon http://10.0.0.5:8085 --tool claude-code`
2. Open Claude Code; confirm mempalace MCP tools visible
3. Run `mempalace_search "test"` → response comes from Artemis palace
