#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

ACCESS_MODE="${TELEAGENT_CODEX_ACCESS_MODE:-full-access}"
if [[ "$TELEAGENT_INSTANCE" == "main" && "$ACCESS_MODE" != "chat-only" ]]; then
  echo "the main relay already uses its configured Codex home" >&2
  exit 2
fi

SOURCE_HOME="$(realpath -m "${TELEAGENT_CODEX_SOURCE_HOME:?missing TELEAGENT_CODEX_SOURCE_HOME}")"
if [[ "$ACCESS_MODE" == "chat-only" ]]; then
  TARGET_HOME="$(realpath -m "${TELEAGENT_CHAT_ONLY_CODEX_HOME:?missing TELEAGENT_CHAT_ONLY_CODEX_HOME}")"
else
  TARGET_HOME="$(realpath -m "${TELEAGENT_CODEX_HOME:?missing TELEAGENT_CODEX_HOME}")"
fi

if [[ "$SOURCE_HOME" == "$TARGET_HOME" ]]; then
  echo "refusing to prepare a Codex home that matches the source home for instance '$TELEAGENT_INSTANCE'" >&2
  exit 1
fi
if [[ -e "$SOURCE_HOME/auth.json" && ! -r "$SOURCE_HOME/auth.json" ]]; then
  echo "source Codex authentication is unreadable: $SOURCE_HOME/auth.json" >&2
  exit 1
fi
if [[ "$ACCESS_MODE" != "chat-only" && ! -r "$SOURCE_HOME/config.toml" ]]; then
  echo "source Codex configuration is unavailable: $SOURCE_HOME/config.toml" >&2
  exit 1
fi

mkdir -p "$TARGET_HOME"
chmod 700 "$TARGET_HOME"
tele_agent_claim_codex_home "$TARGET_HOME"

# CLI authentication is shared deliberately while every conversation/state
# artifact remains private. Codex's file credential backend updates auth.json
# in place, so this link keeps refreshes consistent across isolated homes.
if [[ -r "$SOURCE_HOME/auth.json" ]]; then
  if [[ -L "$TARGET_HOME/auth.json" ]]; then
    if [[ "$(readlink -f "$TARGET_HOME/auth.json")" != "$SOURCE_HOME/auth.json" ]]; then
      echo "private Codex auth link points at the wrong credential file" >&2
      exit 1
    fi
  elif [[ -e "$TARGET_HOME/auth.json" ]]; then
    if ! cmp -s "$SOURCE_HOME/auth.json" "$TARGET_HOME/auth.json"; then
      echo "private Codex auth differs from source; refusing to overwrite it" >&2
      exit 1
    fi
    unlink "$TARGET_HOME/auth.json"
    ln -s "$SOURCE_HOME/auth.json" "$TARGET_HOME/auth.json"
  else
    ln -s "$SOURCE_HOME/auth.json" "$TARGET_HOME/auth.json"
  fi
fi

if [[ "$ACCESS_MODE" == "chat-only" ]]; then
  tele_agent_prepare_chat_only_workspace "$TELEAGENT_CHAT_ONLY_WORKDIR"

  # A chat-only home must not retain capability links from an earlier
  # full-access preparation. Managed links are safe to remove; an unexpected
  # real file or directory fails closed for operator inspection. Codex itself
  # installs bundled system skills under skills/.system on first startup even
  # when every skill feature is disabled. Accept only that marker-backed tree;
  # any user/custom skill beside it still fails closed.
  for shared_name in packages plugins skills rules gh gitconfig vendor_imports; do
    target_path="$TARGET_HOME/$shared_name"
    if [[ -L "$target_path" ]]; then
      unlink "$target_path"
    elif [[ "$shared_name" == "skills" && -d "$target_path" && \
            ! -L "$target_path/.system" && \
            -f "$target_path/.system/.codex-system-skills.marker" && \
            ! -L "$target_path/.system/.codex-system-skills.marker" && \
            -z "$(find "$target_path" -mindepth 1 -maxdepth 1 ! -name .system -print -quit)" ]]; then
      : # Runtime-managed built-ins are inaccessible under the strict config.
    elif [[ -e "$target_path" ]]; then
      echo "chat-only Codex home contains capability data: $target_path" >&2
      exit 1
    fi
  done

  "$SCRIPT_DIR/render_chat_only_codex_config.py" \
    --output "$TARGET_HOME/config.toml" \
    --personality "$TELEAGENT_PERSONALITY_FILE" \
    --trusted-project "$TELEAGENT_CHAT_ONLY_WORKDIR" \
    --provider openai
else
  # Keep a normal instance on the same capability/config surface as the main
  # Codex installation while rewriting absolute home references.
  config_tmp="$(mktemp "$TARGET_HOME/.config.toml.XXXXXX")"
  cleanup() {
    rm -f -- "$config_tmp"
  }
  trap cleanup EXIT
  SOURCE_CODEX_HOME="$SOURCE_HOME" TARGET_CODEX_HOME="$TARGET_HOME" \
    perl -0pe '
      BEGIN {
        $source = $ENV{"SOURCE_CODEX_HOME"};
        $target = $ENV{"TARGET_CODEX_HOME"};
      }
      s/\Q$source\E/$target/g;
    ' "$SOURCE_HOME/config.toml" > "$config_tmp"
  chmod 600 "$config_tmp"
  mv -f -- "$config_tmp" "$TARGET_HOME/config.toml"
  trap - EXIT

  for shared_name in packages plugins skills rules gh gitconfig vendor_imports; do
    source_path="$SOURCE_HOME/$shared_name"
    target_path="$TARGET_HOME/$shared_name"
    [[ -e "$source_path" || -L "$source_path" ]] || continue
    [[ -e "$target_path" || -L "$target_path" ]] || ln -s "$source_path" "$target_path"
  done
fi

for seed_name in models_cache.json version.json; do
  [[ -f "$SOURCE_HOME/$seed_name" ]] || continue
  install -m 600 "$SOURCE_HOME/$seed_name" "$TARGET_HOME/$seed_name"
done

for private_name in sessions history.jsonl session_index.jsonl state_5.sqlite \
  thread_history_1.sqlite goals_1.sqlite memories_1.sqlite; do
  if [[ -L "$TARGET_HOME/$private_name" ]]; then
    echo "private Codex state must not be symlinked: $TARGET_HOME/$private_name" >&2
    exit 1
  fi
done

echo "prepared $ACCESS_MODE Codex home for instance '$TELEAGENT_INSTANCE': $TARGET_HOME"
