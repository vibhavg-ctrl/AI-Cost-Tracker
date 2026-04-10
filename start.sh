#!/bin/bash
# ============================================================
# AI Efficiency Intelligence Platform - Launcher
# Run this script to start the local dashboard
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
BACKEND_PORT=8900
FRONTEND_PORT=8901

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   AI Efficiency Intelligence Platform        ║"
echo "  ║   GSL CEO Office - Local Dashboard           ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 is required. Install from python.org"
    exit 1
fi

# Install dependencies
echo "[1/3] Installing Python dependencies..."
pip3 install fastapi uvicorn pydantic httpx --quiet --break-system-packages 2>/dev/null || \
pip3 install fastapi uvicorn pydantic httpx --quiet 2>/dev/null || \
pip install fastapi uvicorn pydantic --quiet 2>/dev/null

# Kill any existing processes on our ports
echo "[2/3] Clearing ports..."
lsof -ti:$BACKEND_PORT 2>/dev/null | xargs kill -9 2>/dev/null || true
lsof -ti:$FRONTEND_PORT 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

# Start backend
echo "[3/3] Starting services..."
cd "$BACKEND_DIR"
python3 -m uvicorn app:app --host 127.0.0.1 --port $BACKEND_PORT --reload &
BACKEND_PID=$!

# Start frontend (simple HTTP server)
cd "$FRONTEND_DIR"
python3 -m http.server $FRONTEND_PORT --bind 127.0.0.1 &
FRONTEND_PID=$!

sleep 2

echo ""
echo "  ┌─────────────────────────────────────────────┐"
echo "  │  Dashboard:  http://127.0.0.1:$FRONTEND_PORT     │"
echo "  │  API:        http://127.0.0.1:$BACKEND_PORT      │"
echo "  │                                             │"
echo "  │  Press Ctrl+C to stop all services          │"
echo "  └─────────────────────────────────────────────┘"
echo ""

# Open browser (macOS)
if command -v open &>/dev/null; then
    open "http://127.0.0.1:$FRONTEND_PORT"
fi

# Trap Ctrl+C to kill both processes
cleanup() {
    echo ""
    echo "Shutting down..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    echo "Done."
    exit 0
}
trap cleanup INT TERM

# Wait for either process
wait
