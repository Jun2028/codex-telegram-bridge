#!/usr/bin/env bash
set -euo pipefail

if [ "${1-}" != "--" ] || [ "$#" -lt 2 ]; then
  echo "usage: $0 -- COMMAND [ARG ...]" >&2
  exit 2
fi
shift

tmux_test_root=$(mktemp -d /tmp/tele-agent-tmux-test.XXXXXX)
cleanup() {
  # kill-server returns before pane children finish their HUP handlers. Wait
  # for these fixture processes so NFS cleanup cannot race their final writes.
  local fixture_pids
  fixture_pids=$(env -u TMUX TMUX_TMPDIR="$tmux_test_root" \
    tmux list-panes -a -F '#{pane_pid}' 2>/dev/null || true)
  env -u TMUX TMUX_TMPDIR="$tmux_test_root" tmux kill-server \
    >/dev/null 2>&1 || true
  python3 - "$fixture_pids" <<'PY'
import os, sys, time
pids = [int(value) for value in sys.argv[1].split() if value.isdigit()]
deadline = time.monotonic() + 5
while pids and time.monotonic() < deadline:
    alive = []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as handle:
                if handle.read().split(")", 1)[1].split()[0] != "Z":
                    alive.append(pid)
        except OSError:
            pass
    pids = alive
    if pids:
        time.sleep(0.05)
PY
  rmdir "$tmux_test_root" 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

env -u TMUX TMUX_TMPDIR="$tmux_test_root" \
  TELEAGENT_TMUX_TEST_ROOT="$tmux_test_root" "$@"
