#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked
cd frontend
npm ci
npm run build
cd ..
uv run telegram-search setup
uv run telegram-search doctor
