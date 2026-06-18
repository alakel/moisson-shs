#!/usr/bin/env bash
# Smoke test du serveur MCP distant déployé (via le reverse proxy TLS).
#
# Vérifie : (1) /healthz en HTTPS avec certificat valide (sans -k) ; (2) refus
# 401 sans jeton sur /mcp ; (3) handshake initialize authentifié → 200 + session.
#
# Usage :
#   MOISSON_DOMAIN=moisson.example.org MOISSON_API_TOKEN=xxxx ./scripts/smoke_remote.sh
#   ./scripts/smoke_remote.sh moisson.example.org <token>

set -euo pipefail

DOMAIN="${1:-${MOISSON_DOMAIN:-}}"
TOKEN="${2:-${MOISSON_API_TOKEN:-}}"

if [[ -z "$DOMAIN" || -z "$TOKEN" ]]; then
  echo "Usage : $0 <domaine> <token>  (ou via MOISSON_DOMAIN / MOISSON_API_TOKEN)" >&2
  exit 2
fi

BASE="https://${DOMAIN}"
fail() { echo "ÉCHEC : $1" >&2; exit 1; }

echo "==> 1/3 /healthz (HTTPS, certificat valide)"
code=$(curl -fsS -o /dev/null -w '%{http_code}' "${BASE}/healthz") \
  || fail "healthz injoignable ou certificat TLS invalide"
[[ "$code" == "200" ]] || fail "healthz a renvoyé $code (attendu 200)"
echo "    OK (200, chaîne TLS valide)"

echo "==> 2/3 /mcp sans jeton → 401"
code=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "${BASE}/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
[[ "$code" == "401" ]] || fail "attendu 401 sans jeton, obtenu $code"
echo "    OK (401)"

echo "==> 3/3 /mcp initialize authentifié → 200 + Mcp-Session-Id"
hdrs=$(curl -s -D - -o /dev/null \
  -X POST "${BASE}/mcp" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')
echo "$hdrs" | grep -qiE '^HTTP/.* 200' || fail "initialize n'a pas renvoyé 200 :\n$hdrs"
echo "$hdrs" | grep -qi 'mcp-session-id' || echo "    (avertissement : pas de Mcp-Session-Id observé)"
echo "    OK (200)"

echo "==> Smoke test distant : SUCCÈS"
