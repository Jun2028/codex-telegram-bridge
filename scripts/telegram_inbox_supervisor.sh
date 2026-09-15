#!/usr/bin/env bash
# Own the listener child and stop it when its tmux window is replaced.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/relay_paths.sh"
[[ "${1:-}" == "--" && $# -ge 2 ]] || { echo "Usage: $0 -- COMMAND [ARGS...]" >&2; exit 2; }
shift
mkdir -p "$TELEAGENT_LOG_DIR"
log="$TELEAGENT_LOG_DIR/telegram_inbox.supervisor.log"
pid_file="$TELEAGENT_LOG_DIR/telegram_inbox.pid"
# Keep the log open across restarts. Reopening it after ENFILE can terminate
# the supervisor precisely when the listener needs to be restarted.
exec >> "$log" 2>&1
child=""
stopping=0
stop_listener() {
  stopping=1
  [[ -z "$child" ]] || kill -TERM "$child" 2>/dev/null || true
}
trap stop_listener HUP INT TERM
trap 'rm -f "$pid_file" "$pid_file.tmp" || true' EXIT
failures=0
while [[ "$stopping" == 0 ]]; do
  started=$SECONDS
  printf '[%(%Y-%m-%dT%H:%M:%S%z)T] starting telegram inbox\n' -1 || true
  "$@" &
  child=$!
  if ! { printf '%s\n' "$child" > "$pid_file.tmp" &&
         mv -f "$pid_file.tmp" "$pid_file"; }; then
    printf 'Unable to publish listener PID; continuing to supervise pid=%s\n' "$child" || true
    rm -f "$pid_file.tmp" || true
  fi
  result=0
  wait "$child" || result=$?
  if [[ "$stopping" == 1 ]]; then
    wait "$child" 2>/dev/null || true
    break
  fi
  child=""
  rm -f "$pid_file" || true
  (( SECONDS - started < 300 )) || failures=0
  failures=$((failures + 1))
  exponent=$((failures - 1))
  (( exponent < 4 )) || exponent=4
  delay=$((5 * (1 << exponent)))
  (( delay < 60 )) || delay=60
  printf '[%(%Y-%m-%dT%H:%M:%S%z)T] listener exited rc=%s; restarting in %ss\n' -1 "$result" "$delay" || true
  sleep "$delay" &
  child=$!
  wait "$child" || true
  child=""
done
