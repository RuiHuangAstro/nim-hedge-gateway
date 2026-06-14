#!/usr/bin/env bash
# Start/stop the NIM Hedge Gateway Proxy
# Usage: ./proxy.sh start | stop | status

set -euo pipefail

PROXY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROXY_DIR/.venv"
PIDFILE="/tmp/nim-proxy.pid"
LOGFILE="/tmp/nim-proxy.log"
PORT=8000

case "${1:-status}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "NIM proxy already running (PID $(cat "$PIDFILE"))"
      exit 0
    fi
    echo "Starting NIM Hedge Gateway on :$PORT ..."
    cd "$PROXY_DIR"
    if [ ! -x "$VENV/bin/uvicorn" ]; then
      echo "Missing $VENV/bin/uvicorn. Run: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
      exit 1
    fi
    nohup "$VENV/bin/uvicorn" app.main:app \
      --host 127.0.0.1 --port "$PORT" \
      >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Started (PID $(cat "$PIDFILE"))"
    else
      echo "FAILED — check $LOGFILE"
      rm -f "$PIDFILE"
      exit 1
    fi
    ;;

  stop)
    if [ -f "$PIDFILE" ]; then
      PID=$(cat "$PIDFILE")
      kill "$PID" 2>/dev/null && echo "Stopped PID $PID" || echo "Process not found"
      rm -f "$PIDFILE"
    else
      echo "Not running (no PID file)"
    fi
    ;;

  restart)
    $0 stop; sleep 1; $0 start
    ;;

  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      PID=$(cat "$PIDFILE")
      echo "Running (PID $PID)"
      HEALTH=$(curl -s http://127.0.0.1:$PORT/healthz 2>/dev/null || echo "unreachable")
      echo "Health: $HEALTH"
    else
      echo "Not running"
      rm -f "$PIDFILE" 2>/dev/null
    fi
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
