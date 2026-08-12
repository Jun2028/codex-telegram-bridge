#!/usr/bin/env bash
set -euo pipefail

if [ "${1-}" != "--" ] || [ "$#" -lt 2 ]; then
  echo "usage: $0 -- COMMAND [ARG ...]" >&2
  exit 2
fi
shift

tmux_test_root=$(mktemp -d /tmp/tele-agent-tmux-test.XXXXXX)
cleanup() {
  env -u TMUX TMUX_TMPDIR="$tmux_test_root" tmux kill-server \
    >/dev/null 2>&1 || true
  rmdir "$tmux_test_root" 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

env -u TMUX TMUX_TMPDIR="$tmux_test_root" \
  TELEAGENT_TMUX_TEST_ROOT="$tmux_test_root" "$@"
