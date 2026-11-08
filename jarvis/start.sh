#!/usr/bin/env bash
# Startet J.A.R.V.I.S. unter http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec .venv/bin/python server.py
