#!/usr/bin/env bash
# Run from the reviewed release checkout after pushing it.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/relay_paths.sh"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: TELEAGENT_INSTANCE=main scripts/deploy_listener.sh"
  echo "Restarts only this bot's inbox and preserves its Codex pane and state."
  exit 0
fi
[[ $# -eq 0 ]] || { echo "Unexpected arguments; use --help" >&2; exit 2; }
session="${TELEAGENT_TMUX_SESSION:-tele-agent}"
target="${TELEAGENT_INBOX_TARGET:-$session:codex.0}"
window="${TELEAGENT_CODEX_WINDOW:-codex}"
revision="$(git -C "$TELEAGENT_REPO" rev-parse --short HEAD)"
python3 -m compileall -q "$TELEAGENT_REPO/scripts"
mkdir -p "$TELEAGENT_LOG_DIR"
umask 077
agent_before="$(tmux display-message -p -t "$target" '#{pane_id}:#{pane_pid}' 2>/dev/null || true)"
old_listener="$(cat "$TELEAGENT_LOG_DIR/telegram_inbox.pid" 2>/dev/null || true)"
tmux capture-pane -p -t "$session:inbox.0" > "$TELEAGENT_LOG_DIR/listener-before-deploy.txt" 2>/dev/null || true
deployment_flags=()
if [[ "$old_listener" =~ ^[0-9]+$ ]] && [[ -r "/proc/$old_listener/cmdline" ]] &&
   tr '\0' '\n' < "/proc/$old_listener/cmdline" | grep -Fxq -- '--no-agent-watchdog'; then
  deployment_flags+=(--no-agent-watchdog)
fi
started="$(date +%s)"
"$SCRIPT_DIR/start_telegram_inbox.sh" --session "$session" --target-pane "$target" \
  --codex-window "$window" --restart "${deployment_flags[@]}"
python3 - "$TELEAGENT_LOG_DIR" "$started" "$old_listener" "$revision" <<'PY'
import json, os, sys, time
from pathlib import Path
runtime, started, previous, expected = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        pid = runtime.joinpath("telegram_inbox.pid").read_text().strip()
        os.kill(int(pid), 0)
        health = json.loads(runtime.joinpath("telegram_health.state.json").read_text())
        if pid != previous and health.get("revision") == expected and health.get("delivery_ok_ts", 0) >= started:
            print(f"Listener healthy: revision {expected}, pid {pid}")
            break
    except (OSError, ValueError):
        pass
    time.sleep(0.5)
else:
    raise SystemExit("Listener did not become healthy; inspect its supervisor log. Agent was not restarted.")
PY
agent_after="$(tmux display-message -p -t "$target" '#{pane_id}:#{pane_pid}' 2>/dev/null || true)"
[[ "$agent_before" == "$agent_after" ]] || {
  echo "Agent pane identity changed during deployment; inspect the supervisor." >&2
  exit 1
}
printf 'Deployed %s to %s; agent pane preserved.\n' "$revision" "$session"
