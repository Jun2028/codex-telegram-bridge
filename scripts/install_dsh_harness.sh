#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

DSH_VERSION="${TELEAGENT_DSH_VERSION:-0.2.0-rc.2}"
DSH_CLI_ROOT="$TELEAGENT_DSH_HOME/cli"
DSH_BIN_PATH="$DSH_CLI_ROOT/node_modules/.bin/dsh"
DSH_PACKAGE_JSON="$DSH_CLI_ROOT/node_modules/@deepseek-ai/dsh/package.json"
mkdir -p "$TELEAGENT_DSH_HOME"
chmod 700 "$TELEAGENT_DSH_HOME"

if [[ -n "${TELEAGENT_DSH_BIN:-}" ]]; then
  if command -v "$TELEAGENT_DSH_BIN" >/dev/null 2>&1; then
    command -v "$TELEAGENT_DSH_BIN"
    exit 0
  fi
  echo "TELEAGENT_DSH_BIN is not executable: $TELEAGENT_DSH_BIN" >&2
  exit 1
fi

installed_version=""
if [[ -f "$DSH_PACKAGE_JSON" ]] && command -v node >/dev/null 2>&1; then
  installed_version="$(
    node -p 'require(process.argv[1]).version' "$DSH_PACKAGE_JSON" 2>/dev/null \
      || true
  )"
fi
if [[ -x "$DSH_BIN_PATH" && "$installed_version" == "$DSH_VERSION" ]]; then
  printf '%s\n' "$DSH_BIN_PATH"
  exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to install DeepSeek Harness" >&2
  exit 1
fi

mkdir -p "$DSH_CLI_ROOT"
chmod 700 "$DSH_CLI_ROOT"
exec 9>"$DSH_CLI_ROOT/.install.lock"
flock -x 9
installed_version=""
if [[ -f "$DSH_PACKAGE_JSON" ]] && command -v node >/dev/null 2>&1; then
  installed_version="$(
    node -p 'require(process.argv[1]).version' "$DSH_PACKAGE_JSON" 2>/dev/null \
      || true
  )"
fi
if [[ -x "$DSH_BIN_PATH" && "$installed_version" == "$DSH_VERSION" ]]; then
  printf '%s\n' "$DSH_BIN_PATH"
  exit 0
fi

npm install \
  --prefix "$DSH_CLI_ROOT" \
  --no-audit \
  --no-fund \
  --omit=dev \
  "@deepseek-ai/dsh@$DSH_VERSION" >/dev/null

if [[ ! -x "$DSH_BIN_PATH" ]]; then
  echo "DeepSeek Harness install completed without $DSH_BIN_PATH" >&2
  exit 1
fi
printf '%s\n' "$DSH_BIN_PATH"
