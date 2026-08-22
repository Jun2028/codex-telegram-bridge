#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --inspect | --list | --redeem | --test-traversal" >&2
  exit 2
}

mode=${1:-}
case "$mode" in
  --inspect|--list|--redeem|--test-traversal) ;;
  *) usage ;;
esac

runtime=${TELEAGENT_CODEX_RESET_RUNTIME:-$HOME/.local/share/tele-agent/codex-usage-reset-watchdog}
watchdog_script=${TELEAGENT_CODEX_RESET_WATCHDOG_SCRIPT:-$HOME/codex_utils/usage_reset_watchdog.sh}
watchdog_session=${TELEAGENT_CODEX_RESET_WATCHDOG_SESSION:-codex-usage-reset-watchdog}
watchdog_lock="$runtime/state/codex_weekly_reset_watchdog.lock"
manual_lock="$runtime/state/telegram_manual_codex_reset.lock"

case "$runtime" in
  "$HOME"/*) ;;
  *) echo "refusing runtime path outside the user home: $runtime" >&2; exit 2 ;;
esac
[ -x "$watchdog_script" ] || { echo "reset watchdog script is unavailable" >&2; exit 2; }

exec 7>"$manual_lock"
if ! flock -n 7; then
  echo "another manual Codex reset operation is already running" >&2
  exit 4
fi

if tmux list-sessions -F '#{session_name}' 2>/dev/null |
  grep -Eq '^codex-usage-reset-(list-)?[0-9]'; then
  echo "a Codex reset UI is already active; refusing to race it" >&2
  exit 5
fi

restart_watchdog() {
  if tmux has-session -t "$watchdog_session" 2>/dev/null; then
    return
  fi
  local runtime_q script_q command
  printf -v runtime_q '%q' "$runtime"
  printf -v script_q '%q' "$watchdog_script"
  command="TELEAGENT_CODEX_RESET_RUNTIME=$runtime_q exec $script_q"
  tmux new-session -d -s "$watchdog_session" -n tmux -c "$runtime" "$command"
}

cleanup() {
  local rc=$?
  restart_watchdog || true
  exit "$rc"
}
trap cleanup EXIT INT TERM

watchdog_session_is_managed() {
  local pane_pid pane_dead pane_uid arg
  local -a pane_argv=()

  IFS=$'\t' read -r pane_pid pane_dead < <(
    tmux display-message -p -t "$watchdog_session:0.0" \
      '#{pane_pid}	#{pane_dead}' 2>/dev/null
  ) || return 1
  [[ "$pane_pid" =~ ^[0-9]+$ ]] || return 1
  [ "$pane_dead" = "0" ] || return 1
  [ -r "/proc/$pane_pid/cmdline" ] || return 1

  pane_uid=$(stat -c '%u' "/proc/$pane_pid" 2>/dev/null) || return 1
  [ "$pane_uid" = "$(id -u)" ] || return 1

  while IFS= read -r -d '' arg; do
    pane_argv+=("$arg")
  done <"/proc/$pane_pid/cmdline"

  [ "${#pane_argv[@]}" -eq 2 ] &&
    [ "${pane_argv[0]##*/}" = "bash" ] &&
    [ "${pane_argv[1]}" = "$watchdog_script" ]
}

if tmux has-session -t "$watchdog_session" 2>/dev/null; then
  if ! watchdog_session_is_managed; then
    echo "reset watchdog session does not own the expected watchdog process; refusing to replace it" >&2
    exit 6
  fi
  tmux kill-session -t "$watchdog_session"
fi

for _ in {1..120}; do
  if flock -n "$watchdog_lock" true 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if ! flock -n "$watchdog_lock" true 2>/dev/null; then
  echo "automatic reset watchdog did not release its lock" >&2
  exit 7
fi

case "$mode" in
  --inspect)
    TELEAGENT_CODEX_RESET_RUNTIME="$runtime" "$watchdog_script" --inspect-usage
    ;;
  --list)
    TELEAGENT_CODEX_RESET_RUNTIME="$runtime" "$watchdog_script" --list-resets
    ;;
  --redeem)
    TELEAGENT_CODEX_RESET_RUNTIME="$runtime" "$watchdog_script" --manual-reset
    ;;
  --test-traversal)
    TELEAGENT_CODEX_RESET_RUNTIME="$runtime" "$watchdog_script" --test-traversal
    ;;
esac
