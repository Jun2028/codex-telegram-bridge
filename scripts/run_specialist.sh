#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve the owning bot's runtime and authentication home before delegating.
source "$SCRIPT_DIR/relay_paths.sh"
exec python3 "$SCRIPT_DIR/specialist.py" "$@"
