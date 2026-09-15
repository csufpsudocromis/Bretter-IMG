#!/bin/bash
# Bretter-IMG Frontend Startup Script

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"

cd "$(dirname "$0")/frontend"

echo "=== Bretter-IMG Frontend ==="
echo "Console: https://$(hostname -I | awk '{print $1}'):3000"
echo ""

exec npm run dev -- --host 0.0.0.0
