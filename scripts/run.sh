#!/usr/bin/env bash
# Run a factory CLI command through uv, e.g.  ./scripts/run.sh models list
# With no arguments, runs `factory up` (orchestrator + web UI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export FACTORY_ROOT="$ROOT"
export PATH="$HOME/.local/bin:$PATH"

if [[ $# -eq 0 ]]; then
  set -- up
fi
exec uv run --project "$ROOT" factory "$@"
