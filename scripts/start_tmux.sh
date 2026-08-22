#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

SESSION="${1:-tele-agent}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  tele_agent_log "tmux session $SESSION already exists"
else
  tmux new-session -d -s "$SESSION" -c "$TELEAGENT_REPO"
  tmux send-keys -t "$SESSION" "source scripts/relay_paths.sh" C-m
  tele_agent_log "Started tmux session $SESSION at $TELEAGENT_REPO"
fi

tmux display-message -p -t "$SESSION" "session=#{session_name} windows=#{session_windows}"
