#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --locked --extra semantic telegram-search run "$@"
