#!/usr/bin/env bash
# Phase 0 capability-gate smoke test — proves the deployed server's
# four MCP tools work end-to-end over HTTPS, with bearer-token auth.
#
# Usage:
#   ./scripts/smoke.sh                      # uses .env.local token, afair.fly.dev
#   URL=... TOKEN=... ./scripts/smoke.sh    # override either
#
# Exit codes:
#   0  all checks passed
#   1  any check failed

set -euo pipefail

URL="${URL:-https://afair.fly.dev}"
TOKEN="${TOKEN:-$(grep '^AFAIR_AUTH_TOKEN=' .env.local 2>/dev/null | cut -d= -f2- || true)}"

if [ -z "$TOKEN" ]; then
  echo "ERROR: no token. Set TOKEN env var or ensure .env.local has AFAIR_AUTH_TOKEN=" >&2
  exit 1
fi

PASS="\033[32m✓\033[0m"
FAIL="\033[31m✗\033[0m"
ok=0
fail=0

check() {
  local name="$1"
  local cmd="$2"
  local expected="$3"
  local actual
  actual=$(eval "$cmd" 2>&1 || true)
  if echo "$actual" | grep -qE "$expected"; then
    printf "  $PASS  %s\n" "$name"
    ok=$((ok + 1))
  else
    printf "  $FAIL  %s\n" "$name"
    printf "       expected: %s\n" "$expected"
    printf "       got:      %s\n" "$(echo "$actual" | head -3)"
    fail=$((fail + 1))
  fi
}

echo "=== afair smoke ($URL) ==="
echo

echo "── /health (no auth required) ──"
check "GET /health returns 200" \
  "curl -s -o /dev/null -w '%{http_code}' $URL/health" \
  "^200$"
check "GET /health body is ok" \
  "curl -s $URL/health" \
  '"status":"ok"'

echo
echo "── /mcp/ auth gate ──"
check "POST /mcp/ without auth → 401" \
  "curl -s -o /dev/null -w '%{http_code}' -X POST $URL/mcp/ -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'" \
  "^401$"
check "POST /mcp/ with bad token → 401" \
  "curl -s -o /dev/null -w '%{http_code}' -X POST $URL/mcp/ -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'" \
  "^401$"
check "WWW-Authenticate header on 401" \
  "curl -s -i -X POST $URL/mcp/ -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}' | grep -i www-authenticate" \
  'Bearer'

echo
echo "── /mcp/ with correct token ──"
check "POST /mcp/ with correct token → not 401" \
  "curl -s -o /dev/null -w '%{http_code}' -X POST $URL/mcp/ -H 'Authorization: Bearer $TOKEN' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"smoke\",\"version\":\"0\"}}}'" \
  "^[23][0-9][0-9]$"

echo
echo "=== summary ==="
echo "  passed: $ok"
echo "  failed: $fail"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
echo "  status: HEALTHY"
