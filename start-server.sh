#!/bin/bash
# Bretter-IMG Server Startup Script
# Run from the bretter-img/ directory

cd "$(dirname "$0")/server"
export PYTHONPATH="$(pwd)/lib:."

echo "=== Bretter-IMG Server ==="
echo "URL: https://$(hostname -I | awk '{print $1}'):8000"
echo "Docs: https://$(hostname -I | awk '{print $1}'):8000/docs"
echo ""

exec "$(pwd)/lib/bin/uvicorn" app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --reload \
  --ssl-certfile ../.certs/bretter-img.crt \
  --ssl-keyfile ../.certs/bretter-img.key
