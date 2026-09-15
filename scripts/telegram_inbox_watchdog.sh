#!/usr/bin/env bash
# Run in a dedicated tmux window on hosts without a cron/service watchdog.
# Recover a missing inbox window after two successful probes; leave existing
# windows and the Codex agent to their own supervisors and lifecycle controls.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/relay_paths.sh"
[[ $# -eq 0 ]] || { echo "Usage: scripts/telegram_inbox_watchdog.sh" >&2; exit 2; }
session="${TELEAGENT_TMUX_SESSION:-tele-agent}"
window="${TELEAGENT_INBOX_WINDOW:-inbox}"
codex_window="${TELEAGENT_CODEX_WINDOW:-codex}"
target="${TELEAGENT_INBOX_TARGET:-$session:$codex_window.0}"
interval="${TELEAGENT_INBOX_WATCHDOG_INTERVAL:-30}"
[[ "$interval" =~ ^[1-9][0-9]*$ ]] || { echo "Watchdog interval must be a positive integer" >&2; exit 2; }
mkdir -p "$TELEAGENT_LOG_DIR"
exec {watchdog_lock}> "$TELEAGENT_LOG_DIR/telegram_inbox.watchdog.lock"
flock -n "$watchdog_lock" || exit 0
exec >> "$TELEAGENT_LOG_DIR/telegram_inbox.watchdog.log" 2>&1
stopping=0
child=""
stop_watchdog() {
  stopping=1
  [[ -z "$child" ]] || kill -TERM "$child" 2>/dev/null || true
}
trap stop_watchdog HUP INT TERM
log_watchdog() {
  printf '[%(%Y-%m-%dT%H:%M:%S%z)T] %s\n' -1 "$*" || true
}
missing=0
log_watchdog "Watching $session:$window; recovery requires two missing-window probes"
while [[ "$stopping" == 0 ]]; do
  if panes=$(tmux list-panes -a -F '#{session_name}:#{window_name}:#{pane_index}'); then
    panes=$'\n'"$panes"$'\n'
    if [[ "$panes" != *$'\n'"$session:"* ]]; then
      log_watchdog "Managed session is absent; stopping watchdog"
      break
    elif [[ "$panes" == *$'\n'"$session:$window:"* ]]; then
      missing=0
    else
      missing=$((missing + 1))
      if (( missing >= 2 )); then
        log_watchdog "Inbox window is missing; starting listener"
        # Do not use --restart: a concurrent deployment may have already
        # recreated the window between this probe and the launcher check.
        if "$SCRIPT_DIR/start_telegram_inbox.sh" --session "$session" \
          --window "$window" --target-pane "$target" --codex-window "$codex_window"; then
          log_watchdog "Listener started"
        else
          log_watchdog "Listener start failed; will retry after two probes"
        fi
        missing=0
      fi
    fi
  else
    # A failed tmux probe during resource exhaustion is not evidence that
    # the listener is missing. Wait for two fresh successful probes.
    missing=0
    log_watchdog "Unable to inspect tmux; deferring recovery"
  fi
  [[ "$stopping" == 0 ]] || break
  sleep "$interval" &
  child=$!
  wait "$child" || true
  child=""
done
