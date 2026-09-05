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

export TELEAGENT_INSTANCE="${TELEAGENT_INSTANCE:-main}"
# For non-main instances, derive the source home from the main configuration,
# never from a private home inherited during a supervisor re-exec.
if [[ "$TELEAGENT_INSTANCE" != "main" ]]; then
  unset TELEAGENT_CODEX_HOME TELEAGENT_CODEX_SOURCE_HOME
fi
if [[ -f "$TELEAGENT_REPO/config/relay.env" ]]; then
  # shellcheck disable=SC1091
  source "$TELEAGENT_REPO/config/relay.env"
fi

tele_agent_main_codex_home="${TELEAGENT_CODEX_HOME:-$HOME/.codex}"
if [[ "$TELEAGENT_INSTANCE" != "main" ]]; then
  # Never leak another instance's ambient agent/session/config vars into this
  # instance. The instance's own relay-<instance>.env is the override point.
  # A launcher may deliberately pass a freshly registered agent binding to a
  # supervisor. Preserve that binding only when the command-scoped sentinel is
  # present; ordinary ambient bindings are still scrubbed.
  if [[ "${TELEAGENT_PRESERVE_AGENT_BINDING:-0}" != "1" ]]; then
    unset TELEAGENT_AGENT_DIR TELEAGENT_AGENT_ID TELEAGENT_AGENT_JSONL \
      TELEAGENT_AGENT_META TELEAGENT_AGENT_OUTBOX \
      TELEAGENT_AGENT_TARGET_PANE
  fi
  unset TELEAGENT_CODEX_BIN TELEAGENT_CODEX_CHECK_FOR_UPDATE_ON_STARTUP \
    TELEAGENT_CODEX_ACCESS_MODE TELEAGENT_CODEX_HOME TELEAGENT_CODEX_MODEL \
    TELEAGENT_CODEX_SOURCE_HOME TELEAGENT_CHAT_ONLY_CODEX_HOME \
    TELEAGENT_CHAT_ONLY_WORKDIR \
    TELEAGENT_CODEX_REASONING_EFFORT TELEAGENT_CODEX_WINDOW \
    TELEAGENT_DS_CODEX_HOME TELEAGENT_DS_KEY_FILE TELEAGENT_INBOX_TARGET \
    TELEAGENT_LOG_DIR TELEAGENT_SCRATCH TELEAGENT_SECRET_ENV \
    TELEAGENT_PERSONALITY_FILE TELEAGENT_TMUX_SESSION
fi
unset TELEAGENT_PRESERVE_AGENT_BINDING
if [[ "$TELEAGENT_INSTANCE" != "main" && -f "$TELEAGENT_REPO/config/relay-${TELEAGENT_INSTANCE}.env" ]]; then
  # shellcheck disable=SC1091
  source "$TELEAGENT_REPO/config/relay-${TELEAGENT_INSTANCE}.env"
fi

export TELEAGENT_CODEX_ACCESS_MODE="${TELEAGENT_CODEX_ACCESS_MODE:-full-access}"
case "$TELEAGENT_CODEX_ACCESS_MODE" in
  full-access|chat-only) ;;
  *)
    echo "TELEAGENT_CODEX_ACCESS_MODE must be full-access or chat-only" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

if [[ "$TELEAGENT_INSTANCE" == "main" ]]; then
  export TELEAGENT_TMUX_SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
  export TELEAGENT_SCRATCH="${TELEAGENT_SCRATCH:-$HOME/.local/share/tele-agent}"
  export TELEAGENT_LOG_DIR="${TELEAGENT_LOG_DIR:-$TELEAGENT_SCRATCH/runtime}"
  export TELEAGENT_CODEX_HOME="${TELEAGENT_CODEX_HOME:-$HOME/.codex}"
  export TELEAGENT_CODEX_SOURCE_HOME="${TELEAGENT_CODEX_SOURCE_HOME:-$TELEAGENT_CODEX_HOME}"
else
  # Non-main instances must never inherit another instance's ambient paths.
  # Their own relay-<instance>.env is the sanctioned override point.
  export TELEAGENT_TMUX_SESSION="${TELEAGENT_INSTANCE_TMUX_SESSION:-tele-agent-$TELEAGENT_INSTANCE}"
  export TELEAGENT_SCRATCH="${TELEAGENT_INSTANCE_SCRATCH:-$HOME/.local/share/tele-agent-$TELEAGENT_INSTANCE}"
  export TELEAGENT_LOG_DIR="${TELEAGENT_INSTANCE_LOG_DIR:-$TELEAGENT_SCRATCH/runtime}"
  export TELEAGENT_CODEX_SOURCE_HOME="${TELEAGENT_CODEX_SOURCE_HOME:-$tele_agent_main_codex_home}"
  export TELEAGENT_CODEX_HOME="${TELEAGENT_CODEX_HOME:-$TELEAGENT_SCRATCH/codex-home}"

fi

export TELEAGENT_CHAT_ONLY_CODEX_HOME="${TELEAGENT_CHAT_ONLY_CODEX_HOME:-$TELEAGENT_SCRATCH/chat-only-codex-home}"
export TELEAGENT_CHAT_ONLY_WORKDIR="${TELEAGENT_CHAT_ONLY_WORKDIR:-$TELEAGENT_SCRATCH/chat-only-workspace}"

# Full-access non-main instances need private state. Chat-only instances use a
# separate, minimal home so switching modes never exposes a copied capability
# configuration or an earlier full-access conversation store.
tele_agent_source_home_resolved="$(realpath -m "$TELEAGENT_CODEX_SOURCE_HOME")"
if [[ "$TELEAGENT_CODEX_ACCESS_MODE" == "chat-only" ]]; then
  tele_agent_effective_home_resolved="$(realpath -m "$TELEAGENT_CHAT_ONLY_CODEX_HOME")"
  if [[ "$tele_agent_effective_home_resolved" == "$tele_agent_source_home_resolved" ]]; then
    echo "chat-only mode must use a private TELEAGENT_CHAT_ONLY_CODEX_HOME" >&2
    return 1 2>/dev/null || exit 1
  fi
elif [[ "$TELEAGENT_INSTANCE" != "main" ]]; then
  tele_agent_effective_home_resolved="$(realpath -m "$TELEAGENT_CODEX_HOME")"
  if [[ "$tele_agent_effective_home_resolved" == "$tele_agent_source_home_resolved" ]]; then
    echo "non-main instance '$TELEAGENT_INSTANCE' must use a private TELEAGENT_CODEX_HOME" >&2
    return 1 2>/dev/null || exit 1
  fi
fi
unset tele_agent_effective_home_resolved tele_agent_source_home_resolved
unset tele_agent_main_codex_home
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

# A tmux server keeps the environment of the process that created it.  If a
# cron job starts the shared server first, tmux can therefore inherit
# SHELL=/bin/sh even though this project requires Bash.  Always give managed
# panes an explicit Bash command instead of relying on tmux's default-shell.
tele_agent_tmux_bash_shell_command() {
  local bash_bin bash_bin_q
  bash_bin="$(command -v bash)" || {
    echo "bash is required for managed tele-agent tmux panes" >&2
    return 1
  }
  printf -v bash_bin_q '%q' "$bash_bin"
  printf 'exec %s --noprofile --norc' "$bash_bin_q"
}

tele_agent_tmux_bash_command() {
  local command_text="${1:?managed tmux command is required}"
  local bash_bin bash_bin_q command_q
  bash_bin="$(command -v bash)" || {
    echo "bash is required for managed tele-agent tmux panes" >&2
    return 1
  }
  printf -v bash_bin_q '%q' "$bash_bin"
  printf -v command_q '%q' "$command_text"
  printf 'exec %s --noprofile --norc -c %s' "$bash_bin_q" "$command_q"
}

tele_agent_claim_codex_home() {
  local requested_home="${1:?Codex home is required}"
  local instance="${TELEAGENT_INSTANCE:-main}"
  local target_home owner_path lock_path owner owner_tmp
  local owner_fd

  target_home="$(realpath -m "$requested_home")"
  mkdir -p "$target_home"
  chmod 700 "$target_home"
  owner_path="$target_home/.tele-agent-instance"
  lock_path="$target_home/.tele-agent-instance.lock"
  exec {owner_fd}>"$lock_path"
  chmod 600 "$lock_path"
  flock -x "$owner_fd"

  if [[ -e "$owner_path" || -L "$owner_path" ]]; then
    if [[ ! -f "$owner_path" || -L "$owner_path" ]]; then
      echo "invalid Codex home ownership marker: $owner_path" >&2
      exec {owner_fd}>&-
      return 1
    fi
    owner="$(head -n 1 "$owner_path")"
    if [[ "$owner" != "$instance" ]]; then
      echo "Codex home $target_home belongs to tele-agent instance '$owner', not '$instance'" >&2
      exec {owner_fd}>&-
      return 1
    fi
  else
    owner_tmp="$(mktemp "$target_home/.tele-agent-instance.XXXXXX")"
    if ! printf '%s\n' "$instance" > "$owner_tmp" || \
       ! chmod 600 "$owner_tmp" || \
       ! mv -f -- "$owner_tmp" "$owner_path"; then
      rm -f -- "$owner_tmp"
      exec {owner_fd}>&-
      return 1
    fi
  fi
  exec {owner_fd}>&-
}

tele_agent_prepare_chat_only_workspace() {
  local requested_workspace="${1:?chat-only workspace is required}"
  local workspace scratch

  workspace="$(realpath -m "$requested_workspace")"
  scratch="$(realpath -m "$TELEAGENT_SCRATCH")"
  case "$workspace" in
    "$scratch"/*) ;;
    *)
      echo "chat-only workspace must be inside TELEAGENT_SCRATCH" >&2
      return 1
      ;;
  esac
  if [[ -L "$requested_workspace" ]]; then
    echo "chat-only workspace must not be a symlink: $requested_workspace" >&2
    return 1
  fi
  mkdir -p "$workspace"
  chmod 700 "$workspace"
  if find "$workspace" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "chat-only workspace must be empty: $workspace" >&2
    return 1
  fi
}
