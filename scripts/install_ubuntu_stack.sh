#!/usr/bin/env bash
set -euo pipefail

if ! grep -qi "ubuntu" /etc/os-release; then
  echo "This installer is for Ubuntu. Detected:"
  cat /etc/os-release
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/install_cloud_stack.sh" "$@"
