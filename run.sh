#!/usr/bin/env bash
# Run brickremote from a checkout, creating the virtual environment on first use.
#   ./run.sh            remote control app
#   ./run.sh web        web remote control, for any browser on the network
#   ./run.sh scan       any other brickremote command
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
VENV=.venv

if [ ! -x "$VENV/bin/brickremote" ] || ! "$VENV/bin/python" -c "import aiohttp" 2>/dev/null; then
    echo "Setting up $VENV (first run only)..."
    [ -d "$VENV" ] || python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -e ".[web]"
fi

exec "$VENV/bin/brickremote" "$@"
