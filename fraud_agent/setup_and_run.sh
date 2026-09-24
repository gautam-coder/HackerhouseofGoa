#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# TigerGraph Fraud Agent — Hacker House Goa 2026
# Run this to start everything.
# ─────────────────────────────────────────────────────────────────────────────

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "═══════════════════════════════════════════"
echo " TigerGraph Fraud Investigation Agent"
echo " Hacker House Goa 2026"
echo "═══════════════════════════════════════════"

# Load .env
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | grep -v '^$' | xargs)
fi

# 1. Check LLM key
if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ]; then
    echo ""
    echo "ERROR: No LLM API key found."
    echo "  Set ANTHROPIC_API_KEY or OPENAI_API_KEY in fraud_agent/.env"
    echo "  (copy .env.example → .env and fill in your key)"
    exit 1
fi
LLM="${OPENAI_API_KEY:+OpenAI o3}"
echo "✓ LLM: ${LLM}"

# 2. Check TigerGraph
if [ -n "$TG_HOST" ]; then
    echo "✓ TigerGraph: $TG_HOST"
else
    echo "⚠ TigerGraph: NOT configured — cases will be written to SQLite only"
    echo "  Add TG_HOST, TG_USERNAME, TG_PASSWORD to .env for full submission"
fi

# 3. Build local database if needed
if [ ! -f "$SCRIPT_DIR/data/fraud.db" ]; then
    echo ""
    echo "Building local database from CSV files (~3 minutes)..."
    python3 "$SCRIPT_DIR/data/build_db.py"
else
    echo "✓ Local database exists"
fi

# 4. Run investigations
echo ""
echo "Running agent on all 20 cases..."
cd "$SCRIPT_DIR"
python3 run_agent.py --skip-existing

# 5. Start API server in background
echo ""
echo "Starting API server on http://localhost:8000 ..."
uvicorn api:app --port 8000 --host 0.0.0.0 &
API_PID=$!
sleep 2

# 6. Start UI
echo "Starting UI on http://localhost:3000 ..."
cd "$SCRIPT_DIR/ui"
npm run dev &
UI_PID=$!

echo ""
echo "═══════════════════════════════════════════"
echo " Open: http://localhost:3000"
echo " API:  http://localhost:8000"
echo " Press Ctrl+C to stop"
echo "═══════════════════════════════════════════"

wait $API_PID $UI_PID
