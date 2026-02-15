#!/usr/bin/env bash
set -euo pipefail

# DB-only installer wrapper (local PostgreSQL, no Python app/LLM required).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export COMPONENTS="${COMPONENTS:-db}"
export INSTALL_LOCAL_POSTGRES="${INSTALL_LOCAL_POSTGRES:-true}"
export INSTALL_LOCAL_OLLAMA="${INSTALL_LOCAL_OLLAMA:-false}"
export RUN_DB_BOOTSTRAP="${RUN_DB_BOOTSTRAP:-false}"
export INSTALL_SERVICE="${INSTALL_SERVICE:-false}"

bash "$SCRIPT_DIR/install_cloud_stack.sh"
