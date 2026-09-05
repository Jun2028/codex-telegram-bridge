#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
# Test fixtures execute temporary scripts. /tmp may be mounted noexec on HPC.
test_parent="${TELEAGENT_TEST_TMPDIR:-$HOME/.ta-test}"
mkdir -p "$test_parent"
test_parent="$(cd "$test_parent" && pwd -P)"
for script in scripts/*.sh; do
  bash -n "$script"
done
# Runtime settings are host-specific. Tests use their own model/config fixtures.
env -u TELEAGENT_CODEX_MODEL -u TELEAGENT_CODEX_REASONING_EFFORT \
  TMPDIR="$test_parent" "$SCRIPT_DIR/tmux_isolated_test.sh" -- \
  python3 -m unittest discover -s tests -q
