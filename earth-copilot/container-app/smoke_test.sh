#!/bin/bash
# Smoke test for Earth Copilot backend (local, Azure-free)

set -e

API_URL="http://localhost:8000/api/query"
PROMPT='{"query": "Show me satellite imagery of Paris"}'

# Send a test prompt
RESPONSE=$(curl -s -X POST "$API_URL" \
  -H 'Content-Type: application/json' \
  -d "$PROMPT")

if echo "$RESPONSE" | grep -q 'error\|message'; then
  echo "[OK] Backend responded:"
  echo "$RESPONSE"
  exit 0
else
  echo "[FAIL] Unexpected response:"
  echo "$RESPONSE"
  exit 1
fi
