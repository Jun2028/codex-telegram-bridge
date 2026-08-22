#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
AGENT_WINDOW="${TELEAGENT_CODEX_WINDOW:-codex}"
INBOX_WINDOW="${TELEAGENT_INBOX_WINDOW:-inbox}"
TARGET_PANE="$SESSION:$AGENT_WINDOW.0"
LOG_PATH="$TELEAGENT_LOG_DIR/telegram_relay.watchdog.log"
LIFECYCLE_STATE="$TELEAGENT_LOG_DIR/telegram_agent_lifecycle.state.json"

mkdir -p "$TELEAGENT_LOG_DIR"
chmod 700 "$TELEAGENT_LOG_DIR"
exec 9>"$TELEAGENT_LOG_DIR/telegram_relay.watchdog.lock"
flock -n 9 || exit 0

window_exists() {
  local window=$1
  tmux has-session -t "$SESSION" 2>/dev/null \
    && tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null \
      | grep -Fx "$window" >/dev/null
}

agent_healthy() {
  local pane_pid supervisor_pid
  window_exists "$AGENT_WINDOW" || return 1
  pane_pid=$(tmux display-message -p -t "$TARGET_PANE" '#{pane_pid}' 2>/dev/null || true)
  [ -n "$pane_pid" ] || return 1
  supervisor_pid=$(
    ps -o pid=,args= --ppid "$pane_pid" 2>/dev/null \
      | awk '/codex_agent_supervisor[.]sh/{print $1; exit}'
  )
  [ -n "$supervisor_pid" ] || return 1
  ps -o comm= --ppid "$supervisor_pid" 2>/dev/null | grep -Fx codex >/dev/null
}

inbox_healthy() {
  local pane_pid
  window_exists "$INBOX_WINDOW" || return 1
  pane_pid=$(
    tmux display-message -p -t "$SESSION:$INBOX_WINDOW.0" '#{pane_pid}' 2>/dev/null \
      || true
  )
  [ -n "$pane_pid" ] || return 1
  ps -o args= --ppid "$pane_pid" 2>/dev/null \
    | grep -F 'telegram_inbox.py' >/dev/null
}

agent_desired_stopped() {
  [ -f "$LIFECYCLE_STATE" ] || return 1
  [ "$(python3 - "$LIFECYCLE_STATE" <<'PY'
import json
from pathlib import Path
import sys

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = {}
print("yes" if value.get("desired") == "stopped" else "no")
PY
)" = "yes" ]
}

if ! agent_healthy; then
  if agent_desired_stopped; then
    printf '[%s] managed Codex agent is intentionally stopped; leaving it stopped\n' \
      "$(date -Iseconds)" >>"$LOG_PATH"
  else
    printf '[%s] managed Codex agent missing or unhealthy; restarting relay stack\n' \
      "$(date -Iseconds)" >>"$LOG_PATH"
    "$SCRIPT_DIR/start_codex_agent.sh" --session "$SESSION" --window "$AGENT_WINDOW" --restart \
      >>"$LOG_PATH" 2>&1
    exit 0
  fi
fi

if ! inbox_healthy; then
  printf '[%s] Telegram inbox missing or unhealthy; restarting listener\n' \
    "$(date -Iseconds)" >>"$LOG_PATH"
  "$SCRIPT_DIR/start_telegram_inbox.sh" \
    --session "$SESSION" \
    --window "$INBOX_WINDOW" \
    --target-pane "$TARGET_PANE" \
    --codex-window "$AGENT_WINDOW" \
    --restart >>"$LOG_PATH" 2>&1
fi
