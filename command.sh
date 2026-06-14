#!/usr/bin/env bash

APP_MODULE="app.main:app"
HOST="127.0.0.1"
PORT=8000
PYTHON_BIN="${PYTHON_BIN:-python}"

echo "--------------------------------------------------"
echo "Starting nim-hedge-gateway..."
echo "Python: $PYTHON_BIN"
echo "Address: http://$HOST:$PORT"
echo "--------------------------------------------------"

$PYTHON_BIN -m uvicorn $APP_MODULE --host $HOST --port $PORT --log-level info
