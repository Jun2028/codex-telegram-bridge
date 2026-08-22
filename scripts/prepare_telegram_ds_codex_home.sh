#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

DS_UTILS_ROOT="${DS_UTILS_ROOT:-$TELEAGENT_REPO/config/deepseek}"
DS_CODEX_HOME="${TELEAGENT_DS_CODEX_HOME:-$TELEAGENT_SCRATCH/tele-agent-ds-codex-home}"
DS_KEY_FILE="${TELEAGENT_DS_KEY_FILE:-}"

[ -n "$DS_KEY_FILE" ] || {
  echo "TELEAGENT_DS_KEY_FILE is required for DeepSeek mode" >&2
  exit 1
}

[ -f "$DS_UTILS_ROOT/models.json" ] || {
  echo "DeepSeek model catalog is missing: $DS_UTILS_ROOT/models.json" >&2
  exit 1
}
[ -f "$DS_UTILS_ROOT/deepseek-v4-flash.json" ] || {
  echo "DeepSeek flash model specification is missing" >&2
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

mkdir -p "$DS_CODEX_HOME"
chmod 700 "$DS_CODEX_HOME"
install -m 600 "$DS_UTILS_ROOT/models.json" "$DS_CODEX_HOME/models.json"
install -m 600 "$DS_UTILS_ROOT/deepseek-v4-flash.json" \
  "$DS_CODEX_HOME/agent-model.json"

cat > "$DS_CODEX_HOME/config.toml" <<TOML
model = "deepseek-v4-flash"
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

[projects."$TELEAGENT_REPO"]
trust_level = "trusted"
TOML
chmod 600 "$DS_CODEX_HOME/config.toml"

echo "prepared DeepSeek bridge codex home: $DS_CODEX_HOME"
