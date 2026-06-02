#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3.11 -m venv .venv 2>/dev/null || python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example — add your ANTHROPIC_API_KEY."
fi

exec uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
