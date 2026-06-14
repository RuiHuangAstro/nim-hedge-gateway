#!/bin/bash

PORT=8000

echo "Searching for nim-hedge-gateway on port $PORT..."

# IMPORTANT: restrict to LISTEN sockets only. `lsof -i:PORT` without this
# also lists clients with open connections to the port (e.g. hermes), and
# `kill -9` on those takes down the user's session along with the proxy.
PID=$(lsof -t -i:$PORT -sTCP:LISTEN)

if [ ! -z "$PID" ]; then
    echo "Found listener PID $PID. Terminating..."
    kill -9 $PID
    echo "Done."
else
    echo "No listener found on port $PORT."
fi
