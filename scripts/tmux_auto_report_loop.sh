#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/tmux_auto_report_loop.sh [--session tele-agent] [--interval 1800] [--title TITLE] [--max-reports N] [--dry-run]

Sends periodic tmux status reports through scripts/tmux_agent_report.py.
Run this in a tmux window to keep receiving heartbeat reports while jobs run.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
INTERVAL="${TELEAGENT_REPORT_INTERVAL_SECONDS:-1800}"
TITLE="tele-agent heartbeat"
MAX_REPORTS=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      SESSION="${2:-}"
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
    --max-reports)
      MAX_REPORTS="${2:-}"
      shift 2
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

case "$INTERVAL" in
  ''|*[!0-9]*)
    echo "interval must be a positive integer number of seconds" >&2
    exit 2
    ;;
esac
if [[ "$INTERVAL" -lt 60 ]]; then
  echo "interval must be at least 60 seconds" >&2
  exit 2
fi

case "$MAX_REPORTS" in
  ''|*[!0-9]*)
    echo "max reports must be a non-negative integer" >&2
    exit 2
    ;;
esac

tele_agent_log "tmux auto reporter started: session=$SESSION interval=${INTERVAL}s title=$TITLE max_reports=$MAX_REPORTS"

COUNT=0
while true; do
  COUNT=$((COUNT + 1))
  CMD=(
    python3 "$TELEAGENT_REPO/scripts/tmux_agent_report.py"
    --repo-root "$TELEAGENT_REPO"
    --session "$SESSION"
    --title "$TITLE #$COUNT"
    --level info
    --extra "interval_seconds=$INTERVAL"
    --extra "report_index=$COUNT"
  )
  if [[ "$DRY_RUN" -eq 1 ]]; then
    CMD+=(--dry-run)
  fi

  if ! "${CMD[@]}"; then
    tele_agent_log "tmux auto report failed: session=$SESSION index=$COUNT"
  fi

  if [[ "$MAX_REPORTS" -gt 0 && "$COUNT" -ge "$MAX_REPORTS" ]]; then
    break
  fi
  sleep "$INTERVAL"
done

tele_agent_log "tmux auto reporter stopped: session=$SESSION reports=$COUNT"
