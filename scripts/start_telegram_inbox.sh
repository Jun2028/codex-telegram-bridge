#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/start_telegram_inbox.sh [--session tele-agent] [--window inbox] [--target-pane tele-agent:0.0] [--codex-window codex] [--relay-mode tmux-enter] [--submit-delay SECONDS] [--no-agent-watchdog] [--restart]

Starts a dedicated tmux window that polls Telegram for inbound commands.

Accepted Telegram commands:
  /ping
  /status
  /agent_status
  /reauth
  /codex_usage
  /codex_reset
  /Confirm
  /start_agent [LEVEL]
  /kill_agent
  /restart_agent [LEVEL]
  /model MODEL [LEVEL]
  /reasoning LEVEL
  /interrupt PROMPT
  /timed HOURS MESSAGE
  /timed list
  /timed remove [NUMBER]
  /help
  normal text or PDF/TXT/MD document -> relayed to target tmux agent pane

The default relay mode is tmux-enter. It refuses to press Enter into shell panes,
so start Codex/agent in the target pane before sending messages for the agent.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
WINDOW="inbox"
TARGET_PANE="${TELEAGENT_INBOX_TARGET:-tele-agent:0.0}"
CODEX_WINDOW="${TELEAGENT_CODEX_WINDOW:-codex}"
RELAY_MODE="${TELEAGENT_INBOX_MODE:-tmux-enter}"
SUBMIT_DELAY="${TELEAGENT_SUBMIT_DELAY:-2.0}"
RESTART=0
ALLOW_SHELL_PANE=0
PROCESS_EXISTING=0
AGENT_WATCHDOG=1

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
    --target-pane)
      TARGET_PANE="${2:-}"
      shift 2
      ;;
    --codex-window)
      CODEX_WINDOW="${2:-}"
      shift 2
      ;;
    --relay-mode)
      RELAY_MODE="${2:-}"
      shift 2
      ;;
    --submit-delay)
      SUBMIT_DELAY="${2:-}"
      shift 2
      ;;
    --restart)
      RESTART=1
      shift
      ;;
    --allow-shell-pane)
      ALLOW_SHELL_PANE=1
      shift
      ;;
    --process-existing)
      PROCESS_EXISTING=1
      shift
      ;;
    --no-agent-watchdog)
      AGENT_WATCHDOG=0
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

case "$RELAY_MODE" in
  log|tmux-paste|tmux-enter) ;;
  *)
    echo "Invalid --relay-mode: $RELAY_MODE" >&2
    exit 2
    ;;
esac

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  "$SCRIPT_DIR/start_tmux.sh" "$SESSION" >/dev/null
fi

mkdir -p "$TELEAGENT_LOG_DIR" "$TELEAGENT_REPO/logs/readable"

link_readable_path() {
  local name="$1"
  local target="$TELEAGENT_LOG_DIR/$name"
  local link="$TELEAGENT_REPO/logs/readable/$name"
  if [[ -e "$link" && ! -L "$link" ]]; then
    if [[ ! -e "$target" ]]; then
      mv "$link" "$target"
      ln -sfn "$target" "$link"
    else
      printf '%s\n' "$target" > "$link.scratch_path"
    fi
  else
    ln -sfn "$target" "$link"
  fi
}

link_readable_path "telegram_inbox.jsonl"
link_readable_path "telegram_inbox.offset"
link_readable_path "telegram_agent_outbox.jsonl"
link_readable_path "telegram_agent_outbox.offset"
link_readable_path "telegram_agent_messages.state.json"
link_readable_path "telegram_codex_usage.state.json"
link_readable_path "telegram_codex_auth.state.json"
link_readable_path "telegram_codex_reauth.state.json"
link_readable_path "telegram_codex_reset.state.json"
link_readable_path "telegram_agent_lifecycle.state.json"
link_readable_path "telegram_relay_confirmation.state.json"
link_readable_path "telegram_relay_queue.state.json"
link_readable_path "telegram_timed_messages.state.json"
link_readable_path "codex_agent.supervisor.log"
link_readable_path "telegram_inbox.supervisor.log"
link_readable_path "telegram_agents.index.jsonl"

if tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -Fx "$WINDOW" >/dev/null; then
  if [[ "$RESTART" -ne 1 ]]; then
    echo "Telegram inbox window already exists: $SESSION:$WINDOW" >&2
    echo "Use --restart to replace the command in that window." >&2
    exit 2
  fi
  tmux kill-window -t "$SESSION:$WINDOW"
fi

printf -v repo_q '%q' "$TELEAGENT_REPO"
printf -v session_q '%q' "$SESSION"
printf -v target_q '%q' "$TARGET_PANE"
printf -v codex_window_q '%q' "$CODEX_WINDOW"
printf -v mode_q '%q' "$RELAY_MODE"
printf -v submit_delay_q '%q' "$SUBMIT_DELAY"
printf -v telegram_log_dir_q '%q' "$TELEAGENT_LOG_DIR"
printf -v supervisor_log_q '%q' "$TELEAGENT_LOG_DIR/telegram_inbox.supervisor.log"
LISTENER_PID_FILE="$TELEAGENT_LOG_DIR/telegram_inbox.pid"
printf -v listener_pid_file_q '%q' "$LISTENER_PID_FILE"

EXTRA_ARGS=""
if [[ "$ALLOW_SHELL_PANE" -eq 1 ]]; then
  EXTRA_ARGS+=" --allow-shell-pane"
fi
if [[ "$PROCESS_EXISTING" -eq 1 ]]; then
  EXTRA_ARGS+=" --process-existing"
fi
if [[ "$AGENT_WATCHDOG" -ne 1 ]]; then
  EXTRA_ARGS+=" --no-agent-watchdog"
fi

LISTENER_COMMAND="python3 scripts/telegram_inbox.py --session $session_q --target-pane $target_q --codex-window $codex_window_q --relay-mode $mode_q --submit-delay $submit_delay_q$EXTRA_ARGS"
COMMAND="cd $repo_q && source scripts/relay_paths.sh && set +e && mkdir -p $telegram_log_dir_q && while true; do printf '[%s] starting telegram_inbox target=%s mode=%s\n' \"\$(date -Iseconds)\" $target_q $mode_q >> $supervisor_log_q; $LISTENER_COMMAND >> $supervisor_log_q 2>&1 & listener_pid=\$!; printf '%s\n' \"\$listener_pid\" > $listener_pid_file_q; wait \"\$listener_pid\"; rc=\$?; rm -f $listener_pid_file_q; printf '[%s] telegram_inbox exited rc=%s; restarting in 5s\n' \"\$(date -Iseconds)\" \"\$rc\" >> $supervisor_log_q; sleep 5; done"
rm -f "$LISTENER_PID_FILE"
tmux new-window -t "$SESSION" -n "$WINDOW" -c "$TELEAGENT_REPO" "$COMMAND" >/dev/null

listener_started=0
for _ in {1..30}; do
  listener_pid=""
  if [[ -s "$LISTENER_PID_FILE" ]]; then
    IFS= read -r listener_pid < "$LISTENER_PID_FILE" || true
  fi
  if [[ "$listener_pid" =~ ^[0-9]+$ ]] &&
     kill -0 "$listener_pid" 2>/dev/null &&
     grep -aFq "scripts/telegram_inbox.py" "/proc/$listener_pid/cmdline" 2>/dev/null; then
    listener_started=1
    break
  fi
  sleep 0.5
done
if [[ "$listener_started" -ne 1 ]]; then
  echo "Telegram inbox failed to start in $SESSION:$WINDOW." >&2
  tail -n 20 "$TELEAGENT_LOG_DIR/telegram_inbox.supervisor.log" >&2 || true
  exit 1
fi

tele_agent_log "started telegram inbox: session=$SESSION window=$WINDOW target=$TARGET_PANE codex_window=$CODEX_WINDOW mode=$RELAY_MODE submit_delay=$SUBMIT_DELAY"
echo "session=$SESSION window=$WINDOW listener_pid=$listener_pid"
