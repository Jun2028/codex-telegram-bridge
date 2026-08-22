#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TELEAGENT_REPO="${TELEAGENT_REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# Tmux may be created by cron with only /usr/bin:/bin.  Keep the user-managed
# Codex installation discoverable in every relay window, including after boot.
for tele_agent_bin_dir in "$HOME/.npm-global/bin" "$HOME/.local/bin"; do
  if [[ -d "$tele_agent_bin_dir" && ":${PATH:-}:" != *":$tele_agent_bin_dir:"* ]]; then
    PATH="$tele_agent_bin_dir${PATH:+:$PATH}"
  fi
done
export PATH
unset tele_agent_bin_dir

if [[ -f "$TELEAGENT_REPO/config/relay.env" ]]; then
  # shellcheck disable=SC1091
  source "$TELEAGENT_REPO/config/relay.env"
fi

export TELEAGENT_INSTANCE="${TELEAGENT_INSTANCE:-main}"
if [[ "$TELEAGENT_INSTANCE" != "main" ]]; then
  # Never leak another instance's ambient agent/session/config vars into this
  # instance. The instance's own relay-<instance>.env is the override point.
  unset TELEAGENT_AGENT_DIR TELEAGENT_AGENT_ID TELEAGENT_AGENT_JSONL \
    TELEAGENT_AGENT_META TELEAGENT_AGENT_OUTBOX TELEAGENT_AGENT_TARGET_PANE \
    TELEAGENT_CODEX_BIN TELEAGENT_CODEX_HOME TELEAGENT_CODEX_MODEL \
    TELEAGENT_CODEX_REASONING_EFFORT TELEAGENT_CODEX_WINDOW \
    TELEAGENT_DS_CODEX_HOME TELEAGENT_DS_KEY_FILE TELEAGENT_INBOX_TARGET \
    TELEAGENT_LOG_DIR TELEAGENT_SCRATCH TELEAGENT_SECRET_ENV \
    TELEAGENT_PERSONALITY_FILE TELEAGENT_TMUX_SESSION
fi
if [[ "$TELEAGENT_INSTANCE" != "main" && -f "$TELEAGENT_REPO/config/relay-${TELEAGENT_INSTANCE}.env" ]]; then
  # shellcheck disable=SC1091
  source "$TELEAGENT_REPO/config/relay-${TELEAGENT_INSTANCE}.env"
fi

if [[ "$TELEAGENT_INSTANCE" == "main" ]]; then
  export TELEAGENT_TMUX_SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
  export TELEAGENT_SCRATCH="${TELEAGENT_SCRATCH:-$HOME/.local/share/tele-agent}"
  export TELEAGENT_LOG_DIR="${TELEAGENT_LOG_DIR:-$TELEAGENT_SCRATCH/runtime}"
else
  # Non-main instances must never inherit another instance's ambient paths.
  # Their own relay-<instance>.env is the sanctioned override point.
  export TELEAGENT_TMUX_SESSION="${TELEAGENT_INSTANCE_TMUX_SESSION:-tele-agent-$TELEAGENT_INSTANCE}"
  export TELEAGENT_SCRATCH="${TELEAGENT_INSTANCE_SCRATCH:-$HOME/.local/share/tele-agent-$TELEAGENT_INSTANCE}"
  export TELEAGENT_LOG_DIR="${TELEAGENT_INSTANCE_LOG_DIR:-$TELEAGENT_SCRATCH/runtime}"
fi
export TELEAGENT_AGENT_DIR="${TELEAGENT_AGENT_DIR:-$TELEAGENT_LOG_DIR/agents}"

if [[ "$TELEAGENT_INSTANCE" == "main" ]]; then
  export TELEAGENT_SECRET_ENV="${TELEAGENT_SECRET_ENV:-$TELEAGENT_REPO/.secrets/notify.env}"
else
  export TELEAGENT_SECRET_ENV="${TELEAGENT_INSTANCE_SECRET_ENV:-$TELEAGENT_REPO/.secrets/notify-${TELEAGENT_INSTANCE}.env}"
fi

if [[ -n "${TELEAGENT_PERSONALITY_FILE:-}" ]]; then
  :
elif [[ -f "$TELEAGENT_REPO/config/personality-${TELEAGENT_INSTANCE}.md" ]]; then
  export TELEAGENT_PERSONALITY_FILE="$TELEAGENT_REPO/config/personality-${TELEAGENT_INSTANCE}.md"
elif [[ "$TELEAGENT_INSTANCE" == "main" && -f "$TELEAGENT_REPO/config/personality.md" ]]; then
  export TELEAGENT_PERSONALITY_FILE="$TELEAGENT_REPO/config/personality.md"
else
  export TELEAGENT_PERSONALITY_FILE="$TELEAGENT_REPO/config/personality.default.md"
fi

tele_agent_log() {
  local message="$*"
  mkdir -p "$TELEAGENT_LOG_DIR"
  printf '[%s] %s\n' "$(date -Iseconds)" "$message" | tee -a "$TELEAGENT_LOG_DIR/control.log"
}
