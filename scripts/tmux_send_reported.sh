#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/tmux_send_reported.sh --title TITLE [--session tele-agent] COMMAND_STRING

Sends COMMAND_STRING into a tmux session wrapped by tmux_run_with_report.sh.
For complex commands, put them in a small script and pass that script path here.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

TITLE=""
SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --title)
      TITLE="${2:-}"
      shift 2
      ;;
    --session)
      SESSION="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

if [[ -z "$TITLE" || $# -ne 1 ]]; then
  usage
  exit 2
fi

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  "$SCRIPT_DIR/start_tmux.sh" "$SESSION" >/dev/null
fi

COMMAND="$1"
printf -v repo_q '%q' "$TELEAGENT_REPO"
printf -v title_q '%q' "$TITLE"
printf -v command_q '%q' "$COMMAND"
WRAPPED="cd $repo_q && source scripts/relay_paths.sh && scripts/tmux_run_with_report.sh --title $title_q -- bash -lc $command_q"
tmux send-keys -t "$SESSION" "$WRAPPED" C-m
tele_agent_log "sent reported command to tmux session=$SESSION title=$TITLE"
