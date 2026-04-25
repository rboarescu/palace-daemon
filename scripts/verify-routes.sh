#!/usr/bin/env bash
# verify-routes.sh — smoke test for palace-daemon HTTP routes after deploy.
#
# Exercises every public route against a running daemon. Designed to be
# run manually after `systemctl --user restart palace-daemon` or
# equivalent, not in CI (it depends on a live palace).
#
# Usage:
#   PALACE_DAEMON_URL=http://disks.jphe.in:8085 \
#   PALACE_API_KEY=... \
#       scripts/verify-routes.sh

set -e

URL="${PALACE_DAEMON_URL:-http://localhost:8085}"
KEY="${PALACE_API_KEY:-}"
H_AUTH=()
[ -n "$KEY" ] && H_AUTH=(-H "x-api-key: $KEY")

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1" >&2; exit 1; }

probe() {
  local label="$1"
  local expected="$2"
  shift 2
  local resp
  resp=$(curl -sS --max-time 90 "${H_AUTH[@]}" "$@" 2>&1) || fail "$label — curl error"
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
  local val
  val=$(curl -sS --max-time 90 "${H_AUTH[@]}" "$@" 2>&1 | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('$field', ''))
except Exception as e:
    print(f'PARSE-ERROR:{e}', file=sys.stderr)
" 2>&1)
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

# /search — verifies the kind= and limit= params are honored. Earlier
# versions silently dropped limit (passed as max_results) and had no
# kind= support.
probe "GET /search (default kind=content)" "results" "$URL/search?q=palace-daemon&limit=2"
probe "GET /search?kind=all" "results" "$URL/search?q=palace-daemon&limit=2&kind=all"
probe "GET /search?kind=checkpoint" "results" "$URL/search?q=palace-daemon&limit=2&kind=checkpoint"
probe "GET /search rejects bad kind" "must be one of" "$URL/search?q=x&kind=bogus"

# /context — same code path with a different param name for LLM-friendly prompts.
probe "GET /context (default kind=content)" "results" "$URL/context?topic=palace-daemon&limit=2"
probe "GET /context?kind=all" "results" "$URL/context?topic=palace-daemon&limit=2&kind=all"

# /stats — read-only summary across kg + graph + status tools.
probe "GET /stats" "kg" "$URL/stats"

# /repair/status — query state, no actual repair.
probe_json_field "GET /repair/status" "in_progress" "$URL/repair/status"

# limit= is honored — proves the max_results→limit fix landed.
COUNT=$(curl -sS --max-time 90 "${H_AUTH[@]}" "$URL/search?q=palace&limit=3&kind=all" \
  | python3 -c "import json, sys; print(len(json.load(sys.stdin).get('results', [])))" 2>&1)
if [ "$COUNT" = "3" ]; then
  pass "limit=3 returns 3 hits (max_results fix)"
elif [ "$COUNT" = "0" ] || [ -z "$COUNT" ]; then
  echo "  ? limit=3 returned 0 — palace may be empty or unreachable, can't confirm fix"
else
  fail "limit=3 returned $COUNT hits — expected 3 (or 0 on empty palace)"
fi

# Default kind=content excludes more drawers than kind=all.
ALL=$(curl -sS --max-time 90 "${H_AUTH[@]}" "$URL/search?q=palace&limit=20&kind=all" \
  | python3 -c "import json, sys; d=json.load(sys.stdin); print(d.get('available_in_scope', 0))" 2>&1)
CONTENT=$(curl -sS --max-time 90 "${H_AUTH[@]}" "$URL/search?q=palace&limit=20&kind=content" \
  | python3 -c "import json, sys; d=json.load(sys.stdin); print(d.get('available_in_scope', 0))" 2>&1)
if [ "$ALL" -gt "$CONTENT" ] 2>/dev/null; then
  pass "kind=content scope ($CONTENT) < kind=all scope ($ALL) — checkpoint filter active"
elif [ "$ALL" = "0" ] || [ "$ALL" = "$CONTENT" ]; then
  echo "  ? kind=content scope == kind=all scope ($CONTENT) — palace may have no checkpoints to filter"
else
  fail "kind=content scope ($CONTENT) >= kind=all scope ($ALL) — filter is not active"
fi

echo
echo "✓ all routes verified"
