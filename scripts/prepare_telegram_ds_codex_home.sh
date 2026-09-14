#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

DS_UTILS_ROOT="${DS_UTILS_ROOT:-$TELEAGENT_REPO/config/deepseek}"
DS_CODEX_HOME="${TELEAGENT_DS_CODEX_HOME:-$TELEAGENT_SCRATCH/tele-agent-ds-codex-home}"
DS_KEY_FILE="${TELEAGENT_DS_KEY_FILE:-}"
REQUESTED_MODEL="$TELEAGENT_DEEPSEEK_FLASH_MODEL"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      REQUESTED_MODEL="${2:-}"
      shift 2
      ;;
    -h|--help)
      echo "usage: prepare_telegram_ds_codex_home.sh [--model MODEL]" >&2
      exit 0
      ;;
    *)
      echo "usage: prepare_telegram_ds_codex_home.sh [--model MODEL]" >&2
      exit 2
      ;;
  esac
done

[ -n "$DS_KEY_FILE" ] || {
  echo "TELEAGENT_DS_KEY_FILE is required for DeepSeek mode" >&2
  exit 1
}

[ -f "$DS_UTILS_ROOT/models.json" ] || {
  echo "DeepSeek model catalog is missing: $DS_UTILS_ROOT/models.json" >&2
  exit 1
}
[ -r "$DS_KEY_FILE" ] || {
  echo "DeepSeek API environment is unreadable: $DS_KEY_FILE" >&2
  exit 1
}
[ "$(stat -c '%a' "$DS_KEY_FILE")" = 600 ] || {
  echo "DeepSeek API environment must be mode 0600" >&2
  exit 1
}

tele_agent_is_deepseek_model "$REQUESTED_MODEL" || {
  echo "unsupported DeepSeek model: $REQUESTED_MODEL" >&2
  exit 1
}

MODEL_SPEC="$DS_UTILS_ROOT/$REQUESTED_MODEL.json"
[ -f "$MODEL_SPEC" ] || {
  echo "DeepSeek model specification is missing: $MODEL_SPEC" >&2
  exit 1
}

mkdir -p "$DS_CODEX_HOME"
chmod 700 "$DS_CODEX_HOME"
tele_agent_claim_codex_home "$DS_CODEX_HOME"
install -m 600 "$DS_UTILS_ROOT/models.json" "$DS_CODEX_HOME/models.json"
install -m 600 "$MODEL_SPEC" "$DS_CODEX_HOME/agent-model.json"

python3 - "$DS_CODEX_HOME/models.json" "$REQUESTED_MODEL" <<'PY' || {
import json
import sys

catalog, model = sys.argv[1], sys.argv[2]
with open(catalog, encoding="utf-8") as handle:
    slugs = {
        entry.get("slug")
        for entry in json.load(handle).get("models", [])
        if isinstance(entry, dict)
    }
if model not in slugs:
    raise SystemExit(f"DeepSeek model catalog does not list {model}")
PY
  echo "DeepSeek model catalog does not list $REQUESTED_MODEL" >&2
  exit 1
}

if [[ "$TELEAGENT_CODEX_ACCESS_MODE" == "chat-only" ]]; then
  tele_agent_prepare_chat_only_workspace "$TELEAGENT_CHAT_ONLY_WORKDIR"
  for shared_name in packages plugins skills rules gh gitconfig vendor_imports; do
    target_path="$DS_CODEX_HOME/$shared_name"
    if [[ -L "$target_path" ]]; then
      unlink "$target_path"
    elif [[ -e "$target_path" ]]; then
      echo "chat-only Codex home contains capability data: $target_path" >&2
      exit 1
    fi
  done
  "$SCRIPT_DIR/render_chat_only_codex_config.py" \
    --output "$DS_CODEX_HOME/config.toml" \
    --personality "$TELEAGENT_PERSONALITY_FILE" \
    --trusted-project "$TELEAGENT_CHAT_ONLY_WORKDIR" \
    --provider deepseek \
    --model-catalog "$DS_CODEX_HOME/models.json" \
    --model-spec "$DS_CODEX_HOME/agent-model.json"
else
  cat > "$DS_CODEX_HOME/config.toml" <<TOML
model = "$REQUESTED_MODEL"
model_provider = "deepseek"
model_reasoning_effort = "max"
model_catalog_json = "$DS_CODEX_HOME/models.json"
web_search = "live"

[features]
fast_mode = false
goals = true
multi_agent = true
multi_agent_v2 = false
apps = false
plugins = false

[model_providers.deepseek]
name = "deepseek"
base_url = "https://api.deepseek.com/"
wire_api = "responses"
env_key = "DEEPSEEK_API_KEY"

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false

[shell_environment_policy.filters]
"DEEPSEEK_API_KEY" = "exclude"
"OPENAI_API_KEY" = "exclude"

[shell_environment_policy.set]
AGENT_MODEL_SPEC_PATH = "$DS_CODEX_HOME/agent-model.json"
TELEAGENT_MACHINE_CONTEXT = "$TELEAGENT_MACHINE_CONTEXT"
TELEAGENT_INSTANCE_CONTEXT = "$TELEAGENT_INSTANCE_CONTEXT"

[projects."$TELEAGENT_REPO"]
trust_level = "trusted"
TOML
fi
chmod 600 "$DS_CODEX_HOME/config.toml"

echo "prepared $TELEAGENT_CODEX_ACCESS_MODE DeepSeek tele-agent Codex home: $DS_CODEX_HOME model=$REQUESTED_MODEL"
