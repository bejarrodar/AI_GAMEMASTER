#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPONENTS=llm INSTALL_LOCAL_POSTGRES=false INSTALL_LOCAL_OLLAMA=true INSTALL_SERVICE=false RUN_DB_BOOTSTRAP=false "${SCRIPT_DIR}/install_cloud_stack.sh" "$@"
