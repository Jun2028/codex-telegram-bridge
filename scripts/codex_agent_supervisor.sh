#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/codex_agent_supervisor.sh [--model MODEL] [--reasoning-effort LEVEL]

Runs the Telegram Codex agent persistently. Unexpected exits are logged and
restarted with bounded exponential backoff.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

MODEL="${TELEAGENT_CODEX_MODEL:-gpt-5.6-sol}"
REASONING_EFFORT="${TELEAGENT_CODEX_REASONING_EFFORT:-high}"
CODEX_BIN="${TELEAGENT_CODEX_BIN:-codex}"
MAX_RESTARTS="${TELEAGENT_CODEX_SUPERVISOR_MAX_RESTARTS:-0}"
STABLE_SECONDS="${TELEAGENT_CODEX_SUPERVISOR_STABLE_SECONDS:-300}"
BASE_DELAY="${TELEAGENT_CODEX_SUPERVISOR_BASE_DELAY:-5}"
MAX_DELAY="${TELEAGENT_CODEX_SUPERVISOR_MAX_DELAY:-60}"
LOG_PATH="${TELEAGENT_CODEX_SUPERVISOR_LOG:-$TELEAGENT_LOG_DIR/codex_agent.supervisor.log}"
DS_CODEX_HOME="${TELEAGENT_DS_CODEX_HOME:-$TELEAGENT_SCRATCH/tele-agent-ds-codex-home}"
DS_KEY_FILE="${TELEAGENT_DS_KEY_FILE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      MODEL="${2:-}"
      shift 2
      ;;
    --reasoning-effort)
      REASONING_EFFORT="${2:-}"
      shift 2
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

if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
  echo "codex CLI not found: $CODEX_BIN" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG_PATH")"

stop_requested=0
child_pid=""

stop_supervisor() {
  stop_requested=1
  if [[ -n "$child_pid" ]]; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
}

trap stop_supervisor INT TERM HUP

failure_count=0
restart_count=0
while [[ "$stop_requested" -eq 0 ]]; do
  started_at="$(date +%s)"
  printf '[%s] starting codex model=%s reasoning=%s restart_count=%s\n' \
    "$(date -Iseconds)" "$MODEL" "$REASONING_EFFORT" "$restart_count" >> "$LOG_PATH"

  use_ds=0
  case "$MODEL" in
    deepseek-v4-flash|deepseek-v4-pro) use_ds=1 ;;
  esac
  if [[ "$use_ds" -eq 1 ]]; then
    "$SCRIPT_DIR/prepare_telegram_ds_codex_home.sh" >/dev/null
    export CODEX_HOME="$DS_CODEX_HOME"
    set -a
    # shellcheck disable=SC1090
    source "$DS_KEY_FILE"
    set +a
    if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -n "${OPENAI_API_KEY:-}" ]; then
      export DEEPSEEK_API_KEY="$OPENAI_API_KEY"
    fi
    unset OPENAI_API_KEY OPENAI_BASE_URL || true
  else
    unset CODEX_HOME DEEPSEEK_API_KEY OPENAI_BASE_URL || true
  fi

  codex_args=(
    --model "$MODEL"
    -c "model_reasoning_effort=\"$REASONING_EFFORT\""
    --no-alt-screen
    --sandbox danger-full-access
    --ask-for-approval never
    --cd "$TELEAGENT_REPO"
  )
  if [[ "$use_ds" -eq 0 ]]; then
    codex_args+=(--enable fast_mode)
  fi
  set +e
  # Codex is an interactive TUI and must remain the foreground process for the
  # tmux pane. Starting it as a background job disconnects stdin from the TTY
  # and makes Codex exit with "stdin is not a terminal".
  "$CODEX_BIN" "${codex_args[@]}"
  rc=$?
  set -e

  [[ "$stop_requested" -eq 0 ]] || break

  ended_at="$(date +%s)"
  runtime=$((ended_at - started_at))
  if (( runtime >= STABLE_SECONDS )); then
    failure_count=0
  fi
  failure_count=$((failure_count + 1))
  restart_count=$((restart_count + 1))
  backoff_exponent=$((failure_count - 1))
  (( backoff_exponent > 10 )) && backoff_exponent=10
  delay=$((BASE_DELAY * (1 << backoff_exponent)))
  (( delay > MAX_DELAY )) && delay=$MAX_DELAY

  printf '[%s] codex exited rc=%s runtime=%ss; restarting in %ss\n' \
    "$(date -Iseconds)" "$rc" "$runtime" "$delay" >> "$LOG_PATH"

  if (( failure_count == 1 || failure_count % 5 == 0 )); then
    "$SCRIPT_DIR/telegram_agent_reply.sh" progress \
      "Codex agent exited unexpectedly (rc=$rc); restarting automatically in ${delay}s." \
      >/dev/null 2>&1 || true
  fi

  if (( MAX_RESTARTS > 0 && restart_count >= MAX_RESTARTS )); then
    printf '[%s] maximum restart count reached; supervisor exiting\n' "$(date -Iseconds)" >> "$LOG_PATH"
    break
  fi
  sleep "$delay" &
  child_pid=$!
  wait "$child_pid" || true
  child_pid=""
done

printf '[%s] codex supervisor stopped\n' "$(date -Iseconds)" >> "$LOG_PATH"
