#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked --extra semantic
cd frontend
npm ci
npm run build
cd ..
uv run --no-sync telegram-search setup
uv run --no-sync telegram-search doctor
