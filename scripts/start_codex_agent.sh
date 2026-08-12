#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/start_codex_agent.sh [--session tele-agent] [--window codex] [--restart]

Starts an interactive Codex CLI agent in a tmux window and
restarts the Telegram inbox so normal Telegram text is relayed to that Codex pane.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
WINDOW="codex"
RESTART=0
CODEX_MODEL="${TELEAGENT_CODEX_MODEL:-gpt-5.6-sol}"
CODEX_REASONING_EFFORT="${TELEAGENT_CODEX_REASONING_EFFORT:-high}"

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
    --restart)
      RESTART=1
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

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI not found on PATH" >&2
  exit 1
fi

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  "$SCRIPT_DIR/start_tmux.sh" "$SESSION" >/dev/null
fi

if tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -Fx "$WINDOW" >/dev/null; then
  if [[ "$RESTART" -eq 1 ]]; then
    tmux kill-window -t "$SESSION:$WINDOW"
    tmux new-window -t "$SESSION" -n "$WINDOW" -c "$TELEAGENT_REPO" >/dev/null
    CODEX_ALREADY_RUNNING=0
  else
    CURRENT_CMD="$(tmux display-message -p -t "$SESSION:$WINDOW.0" '#{pane_current_command}' 2>/dev/null || true)"
    PANE_PID="$(tmux display-message -p -t "$SESSION:$WINDOW.0" '#{pane_pid}' 2>/dev/null || true)"
    SUPERVISOR_RUNNING=0
    if [[ -n "$PANE_PID" ]] && ps -o args= --ppid "$PANE_PID" 2>/dev/null | grep -F 'codex_agent_supervisor.sh' >/dev/null; then
      SUPERVISOR_RUNNING=1
    fi
    if [[ "$CURRENT_CMD" == "codex" || "$SUPERVISOR_RUNNING" -eq 1 ]]; then
      CODEX_ALREADY_RUNNING=1
    elif [[ "$CURRENT_CMD" != "bash" && "$CURRENT_CMD" != "sh" && "$CURRENT_CMD" != "zsh" && "$CURRENT_CMD" != "fish" ]]; then
      echo "Codex window already appears busy: $SESSION:$WINDOW.0 cmd=$CURRENT_CMD" >&2
      echo "Use --restart to interrupt and start a fresh Codex process." >&2
      exit 2
    else
      CODEX_ALREADY_RUNNING=0
    fi
  fi
else
  tmux new-window -t "$SESSION" -n "$WINDOW" -c "$TELEAGENT_REPO" >/dev/null
  CODEX_ALREADY_RUNNING=0
fi

printf -v repo_q '%q' "$TELEAGENT_REPO"
printf -v codex_model_q '%q' "$CODEX_MODEL"
printf -v codex_reasoning_q '%q' "$CODEX_REASONING_EFFORT"
printf -v supervisor_q '%q' "$SCRIPT_DIR/codex_agent_supervisor.sh"
TARGET_PANE="$SESSION:$WINDOW.0"

if [[ "${CODEX_ALREADY_RUNNING:-0}" -ne 1 ]]; then
  DS_EXTRA=""
  if [[ "$CODEX_MODEL" == deepseek-v4-flash || "$CODEX_MODEL" == deepseek-v4-pro ]]; then
    DS_KEY_FILE="${TELEAGENT_DS_KEY_FILE:-}"
    if [[ -z "$DS_KEY_FILE" || ! -r "$DS_KEY_FILE" ]]; then
      echo "TELEAGENT_DS_KEY_FILE must point to a readable DeepSeek key file for model $CODEX_MODEL" >&2
      exit 1
    fi
    DS_CODEX_HOME="${TELEAGENT_DS_CODEX_HOME:-$TELEAGENT_SCRATCH/tele-agent-ds-codex-home}"
    if [[ ! -f "$DS_CODEX_HOME/config.toml" ]]; then
      echo "DeepSeek Codex home is not prepared; run scripts/prepare_telegram_ds_codex_home.sh first" >&2
      exit 1
    fi
    printf -v ds_key_q '%q' "$DS_KEY_FILE"
    printf -v ds_home_q '%q' "$DS_CODEX_HOME"
    DS_EXTRA="source $ds_key_q && export CODEX_HOME=$ds_home_q && "
  fi
  START_EPOCH="$(date +%s)"
  eval "$(
    python3 "$SCRIPT_DIR/telegram_agent_registry.py" create \
      --repo-root "$TELEAGENT_REPO" \
      --session "$SESSION" \
      --window "$WINDOW" \
      --target-pane "$TARGET_PANE" \
      --launch-source "start_codex_agent.sh" \
      --start-epoch "$START_EPOCH" \
      --shell-exports
  )"
  printf -v agent_id_q '%q' "$TELEAGENT_AGENT_ID"
  printf -v agent_jsonl_q '%q' "$TELEAGENT_AGENT_JSONL"
  printf -v agent_meta_q '%q' "$TELEAGENT_AGENT_META"
  printf -v agent_outbox_q '%q' "$TELEAGENT_AGENT_OUTBOX"
  printf -v agent_target_q '%q' "$TELEAGENT_AGENT_TARGET_PANE"
  printf -v telegram_log_dir_q '%q' "$TELEAGENT_LOG_DIR"
  CODEX_AGENT_ENV="TELEAGENT_AGENT_ID=$agent_id_q TELEAGENT_AGENT_JSONL=$agent_jsonl_q TELEAGENT_AGENT_META=$agent_meta_q TELEAGENT_AGENT_OUTBOX=$agent_outbox_q TELEAGENT_AGENT_TARGET_PANE=$agent_target_q TELEAGENT_LOG_DIR=$telegram_log_dir_q"
  CODEX_COMMAND="cd $repo_q && source scripts/relay_paths.sh && ${DS_EXTRA}$CODEX_AGENT_ENV $supervisor_q --model $codex_model_q --reasoning-effort $codex_reasoning_q"

  tmux send-keys -t "$TARGET_PANE" "$CODEX_COMMAND" C-m
  for _ in {1..20}; do
    PANE_PID="$(tmux display-message -p -t "$TARGET_PANE" '#{pane_pid}' 2>/dev/null || true)"
    # With pipefail enabled, ps returns 1 during the short interval before the
    # pane has a child.  Treat that as "not ready yet" instead of aborting the
    # launcher before it can create the Telegram inbox.
    SUPERVISOR_PID="$(
      ps -o pid=,args= --ppid "$PANE_PID" 2>/dev/null \
        | awk '/codex_agent_supervisor[.]sh/{print $1; exit}' \
        || true
    )"
    if [[ -n "$SUPERVISOR_PID" ]] && ps -o comm= --ppid "$SUPERVISOR_PID" 2>/dev/null | grep -Fx 'codex' >/dev/null; then
      break
    fi
    sleep 0.5
  done
  python3 "$SCRIPT_DIR/telegram_agent_registry.py" link \
    --agent-id "$TELEAGENT_AGENT_ID" \
    --target-pane "$TARGET_PANE" \
    --start-epoch "$START_EPOCH" >/dev/null || true
else
  eval "$(
    python3 "$SCRIPT_DIR/telegram_agent_registry.py" adopt \
      --repo-root "$TELEAGENT_REPO" \
      --session "$SESSION" \
      --window "$WINDOW" \
      --target-pane "$TARGET_PANE" \
      --launch-source "start_codex_agent.sh-reuse" \
      --shell-exports
  )"
fi

LIFECYCLE_STATE="$TELEAGENT_LOG_DIR/telegram_agent_lifecycle.state.json"
python3 - "$LIFECYCLE_STATE" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile
import time

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "version": 1,
            "desired": "running",
            "source": "start_codex_agent.sh",
            "updated_ts": int(time.time()),
        },
        handle,
        sort_keys=True,
    )
    handle.write("\n")
os.chmod(temporary_name, 0o600)
os.replace(temporary_name, path)
PY

"$SCRIPT_DIR/start_telegram_inbox.sh" \
  --session "$SESSION" \
  --target-pane "$TARGET_PANE" \
  --restart >/dev/null

if [[ "${CODEX_ALREADY_RUNNING:-0}" -eq 1 ]]; then
  tele_agent_log "codex agent already running; restarted telegram inbox: session=$SESSION window=$WINDOW target=$TARGET_PANE"
else
  tele_agent_log "started codex agent: session=$SESSION window=$WINDOW target=$TARGET_PANE"
fi
PANE_PID="$(tmux display-message -p -t "$TARGET_PANE" '#{pane_pid}' 2>/dev/null || true)"
SUPERVISOR_PID="$(
  ps -o pid=,args= --ppid "$PANE_PID" 2>/dev/null \
    | awk '/codex_agent_supervisor[.]sh/{print $1; exit}' \
    || true
)"
if [[ -n "$SUPERVISOR_PID" ]] && ps -o comm= --ppid "$SUPERVISOR_PID" 2>/dev/null | grep -Fx 'codex' >/dev/null; then
  echo "session=$SESSION window=$WINDOW pane=0 cmd=codex supervised=yes model=$CODEX_MODEL reasoning=$CODEX_REASONING_EFFORT"
else
  echo "session=$SESSION window=$WINDOW pane=0 cmd=unhealthy supervised=no" >&2
  exit 1
fi
