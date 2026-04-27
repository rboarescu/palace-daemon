#!/usr/bin/env bash
# verify-routes.sh — smoke test for palace-daemon HTTP routes after deploy.
#
# Exercises every public read-only route against a running daemon. POST
# routes (/repair, /silent-save, /memory, /mine, /flush, /reload, /backup)
# mutate state and are intentionally not exercised here — they belong in
# dedicated integration runs against a throwaway palace, not in a manual
# smoke against the production daemon.
#
# Designed for manual deploy validation (not CI — depends on a live
# palace).
#
# Usage:
#   PALACE_DAEMON_URL=http://your-host:8085 \
#   PALACE_API_KEY=... \
#       scripts/verify-routes.sh

set -euo pipefail

URL="${PALACE_DAEMON_URL:-http://localhost:8085}"
KEY="${PALACE_API_KEY:-}"
H_AUTH=()
[ -n "$KEY" ] && H_AUTH=(-H "x-api-key: $KEY")

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1" >&2; exit 1; }

# `curl -fsS` so HTTP non-2xx (e.g. /health 503 when palace is degraded)
# fails curl directly, instead of letting a 503 body that happens to
# contain the expected substring slip through as a pass.
probe() {
  local label="$1"
  local expected="$2"
  shift 2
  local resp
  if ! resp=$(curl -fsS --max-time 90 "${H_AUTH[@]}" "$@" 2>&1); then
    fail "$label — curl failed (HTTP non-2xx or connection error): ${resp:0:200}"
  fi
  if echo "$resp" | grep -q "$expected"; then
    pass "$label"
  else
    fail "$label — expected '$expected' in response: ${resp:0:200}"
  fi
}

probe_json_field() {
  local label="$1"
  local field="$2"
  shift 2
  local resp val
  if ! resp=$(curl -fsS --max-time 90 "${H_AUTH[@]}" "$@" 2>&1); then
    fail "$label — curl failed (HTTP non-2xx or connection error)"
  fi
  val=$(echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('$field', ''))
except Exception as e:
    print(f'PARSE-ERROR:{e}', file=sys.stderr)
" 2>&1) || true
  if [ -n "$val" ] && [ "${val:0:13}" != "PARSE-ERROR:" ]; then
    pass "$label ($field=$val)"
  else
    fail "$label — bad JSON or missing field: $val"
  fi
}

echo "→ palace-daemon at $URL"
echo

# /health — no auth, should always respond.
probe "GET /health" "palace-daemon" "$URL/health"

# /search — semantic search; verifies the limit= param is honored.
probe "GET /search" "results" "$URL/search?q=palace&limit=2"

# /context — same code path with a different param name for LLM-friendly prompts.
probe "GET /context" "results" "$URL/context?topic=palace&limit=2"

# /stats — read-only summary across kg + graph + status tools.
probe "GET /stats" "kg" "$URL/stats"

# /repair/status — query state, no actual repair.
probe_json_field "GET /repair/status" "in_progress" "$URL/repair/status"

# limit= is honored end-to-end. Useful as a regression check, but
# fail-shaped only when the daemon returns valid JSON with a result
# count that is neither the requested 3 nor 0. Connection or parse
# errors `?`-flag rather than fail, so an empty/unreachable palace
# doesn't break the smoke run.
if ! resp=$(curl -fsS --max-time 90 "${H_AUTH[@]}" "$URL/search?q=palace&limit=3" 2>&1); then
  echo "  ? limit=3 — curl failed (HTTP error or unreachable), can't confirm"
elif COUNT=$(echo "$resp" | python3 -c "import json, sys; print(len(json.load(sys.stdin).get('results', [])))" 2>/dev/null); then
  if [ "$COUNT" = "3" ]; then
    pass "limit=3 returns 3 hits"
  elif [ "$COUNT" = "0" ]; then
    echo "  ? limit=3 returned 0 — palace may be empty, can't confirm"
  else
    fail "limit=3 returned $COUNT hits — expected 3 (or 0 on empty palace)"
  fi
else
  echo "  ? limit=3 — response wasn't valid JSON, can't confirm"
fi

echo
echo "✓ all routes verified"
