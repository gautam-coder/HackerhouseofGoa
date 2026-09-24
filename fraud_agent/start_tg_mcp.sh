#!/bin/bash
# Start the TigerGraph MCP server (HTTP/SSE mode on port 8001)
# The agent calls TigerGraph tools through this server.
#
# Required in .env:
#   TG_HOST=https://your-workspace.i.tgcloud.io
#   TG_USERNAME=tigergraph
#   TG_PASSWORD=your-password
#   TG_GRAPHNAME=FraudGraph

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | grep -v '^$' | xargs)
fi

if [ -z "$TG_HOST" ]; then
    echo "ERROR: TG_HOST not set in $SCRIPT_DIR/.env"
    echo "  Sign up at https://savanna.tgcloud.io, create a workspace,"
    echo "  then add TG_HOST, TG_USERNAME, TG_PASSWORD to .env"
    exit 1
fi

echo "Starting TigerGraph MCP server..."
echo "  Host: $TG_HOST"
echo "  Graph: $TG_GRAPHNAME"
echo "  MCP endpoint: http://localhost:8001/mcp"
echo ""

python3 -m tigergraph_mcp.main \
    --transport streamable-http \
    --host 0.0.0.0 \
    --port 8001 \
    --mount-path /mcp \
    --env-file "$SCRIPT_DIR/.env" \
    -v
