# Security Policy

## Threat Model

palace-daemon is a **local-network service** designed to run on a trusted home server and be accessed only by machines you control. It is not designed to be exposed to the internet.

## Daemon-Only Access

All palace clients (Claude Code, Gemini CLI, VSCode, Cursor) must communicate exclusively through the daemon's HTTP API. Direct access to the ChromaDB files or SQLite database from client machines is not supported and bypasses all concurrency controls.

## API Key

palace-daemon supports optional API key authentication via the `PALACE_API_KEY` environment variable. When set, every request must include the header `X-API-Key: <key>`.

**Recommendation:** Always set `PALACE_API_KEY` if your daemon is reachable by more than one machine on your network.

```bash
# In palace-daemon.service or your shell profile:
Environment=PALACE_API_KEY=<your-key>
```

The key is re-read from the environment on every request, so it can be rotated without restarting the daemon.

## Network Exposure

- Bind to `0.0.0.0` only on networks you trust (home LAN, VPN).
- If you need remote access over an untrusted network, put the daemon behind a reverse proxy with TLS (e.g. nginx + Let's Encrypt) rather than exposing port 8085 directly.
- The `/mine` endpoint runs a subprocess against a filesystem path. It validates that the path is absolute, exists, and contains no traversal components, but it should still be treated as a privileged operation — restrict it with `PALACE_API_KEY`.

## NFS / Network Filesystems

Do **not** place the palace directory (`~/.mempalace/palace`) on an NFS mount or any network filesystem. ChromaDB's SQLite database uses `fcntl` file locking, which is unreliable over NFS and can corrupt the database under concurrent access.

## Lock File

The daemon writes a lock file to `~/.cache/palace-daemon/daemon-{port}.lock` (directory mode `0o700`) to prevent multiple instances from binding to the same port. Do not run palace-daemon as root.

## Reporting Issues

Open an issue at https://github.com/rboarescu/palace-daemon/issues. For sensitive findings, use GitHub's private vulnerability reporting.
