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

export TELEAGENT_SCRATCH="${TELEAGENT_SCRATCH:-$HOME/.local/share/tele-agent}"
export TELEAGENT_LOG_DIR="${TELEAGENT_LOG_DIR:-$TELEAGENT_SCRATCH/runtime}"
export TELEAGENT_AGENT_DIR="${TELEAGENT_AGENT_DIR:-$TELEAGENT_LOG_DIR/agents}"

tele_agent_log() {
  local message="$*"
  mkdir -p "$TELEAGENT_LOG_DIR"
  printf '[%s] %s\n' "$(date -Iseconds)" "$message" | tee -a "$TELEAGENT_LOG_DIR/control.log"
}
