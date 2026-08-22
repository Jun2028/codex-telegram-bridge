#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/start_tmux_auto_reporter.sh [--session tele-agent] [--window reporter] [--interval 1800] [--title TITLE] [--restart] [--dry-run]

Starts a dedicated tmux window that sends periodic Telegram/email status reports.
Use --restart to replace an existing reporter window command.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
WINDOW="reporter"
INTERVAL="${TELEAGENT_REPORT_INTERVAL_SECONDS:-1800}"
TITLE="bridge heartbeat"
RESTART=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      SESSION="${2:-}"
      shift 2
      ;;
    --window)
      WINDOW="${2:-}"
      shift 2
      ;;
    --interval)
      INTERVAL="${2:-}"
      shift 2
      ;;
    --title)
      TITLE="${2:-}"
      shift 2
      ;;
    --restart)
      RESTART=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  "$SCRIPT_DIR/start_tmux.sh" "$SESSION" >/dev/null
fi

if tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -Fx "$WINDOW" >/dev/null; then
  if [[ "$RESTART" -ne 1 ]]; then
    echo "Reporter window already exists: $SESSION:$WINDOW" >&2
    echo "Use --restart to replace the command in that window." >&2
    exit 2
  fi
  tmux send-keys -t "$SESSION:$WINDOW" C-c
else
  tmux new-window -t "$SESSION" -n "$WINDOW" -c "$TELEAGENT_REPO" >/dev/null
fi

printf -v repo_q '%q' "$TELEAGENT_REPO"
printf -v session_q '%q' "$SESSION"
printf -v interval_q '%q' "$INTERVAL"
printf -v title_q '%q' "$TITLE"
DRY_ARG=""
if [[ "$DRY_RUN" -eq 1 ]]; then
  DRY_ARG=" --dry-run"
fi

COMMAND="cd $repo_q && source scripts/relay_paths.sh && scripts/tmux_auto_report_loop.sh --session $session_q --interval $interval_q --title $title_q$DRY_ARG"
tmux send-keys -t "$SESSION:$WINDOW" "$COMMAND" C-m
tele_agent_log "started tmux auto reporter: session=$SESSION window=$WINDOW interval=${INTERVAL}s"
tmux display-message -p -t "$SESSION:$WINDOW" "session=#{session_name} window=#{window_name}"
