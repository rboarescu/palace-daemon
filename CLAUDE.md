# Claude Code Protocols

## Core Mandates

### 1. SSH-Friendly Feedback
- **Always** provide a concise, one-line terminal confirmation (e.g., '📥 Filed to {room}') after filing memories via the MemPalace MCP.
- Do not rely on desktop notifications as the user is often on SSH.

### 2. Post-Phase Documentation
- At the end of every work phase, systematically update the project's `README.md` or `CHANGELOG.md`.
- **Mandatory:** File a roadmap update to the corresponding room in the 'lab_projects' wing via MemPalace.

### 3. Service Management
- **System Service Only:** ALWAYS manage `palace-daemon` via `sudo systemctl [start|stop|restart] palace-daemon`.
- **No Manual Starts:** NEVER start the daemon manually via `python3 main.py`. Manual startup is blocked by default and requires the `--manual` flag; only use this for isolated debugging.

### 4. Memory Protocol
- **Silent Mode:** Ensure `silent_save` is enabled in MemPalace settings to prevent blocking the chat flow.
- **Roadmap Sync:** Before finishing, check the 'lab_projects' wing to ensure the next steps are clearly documented for the next session.

### 5. Upgrading mempalace
After `pipx upgrade mempalace`, always re-apply local patches and restart:

    bash /home/radu/palace-daemon/scripts/apply_patches.sh
    sudo systemctl restart palace-daemon

If a patch conflicts, the script will say so. Check whether upstream fixed the issue — if so, delete the patch file. Otherwise update the patch to match the new code.

Patches live in `patches/`. Current patches:
- `mcp_server_get_collection.patch` — `_get_collection`: exception logging, auto-retry on cache failure, `hnsw:num_threads=1` enforcement (workaround for ChromaDB issue #1161)
