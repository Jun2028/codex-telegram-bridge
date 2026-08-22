#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

step() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

if [ "$(uname -s)" != "Linux" ]; then
  die "this bootstrap supports Linux only"
fi

case "$(uname -m)" in
  x86_64) codex_target="x86_64-unknown-linux-musl" ;;
  aarch64|arm64) codex_target="aarch64-unknown-linux-musl" ;;
  *) die "unsupported Linux architecture: $(uname -m)" ;;
esac

step "Checking prerequisites (python3, git, tmux, curl, tar, flock)"
missing=""
for tool in python3 git tmux curl tar flock; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
  echo "Missing:$missing"
  privilege=""
  if [ "$(id -u)" -eq 0 ]; then
    privilege=""
  elif command -v sudo >/dev/null 2>&1; then
    privilege="sudo"
  else
    die "install the missing packages as root, then rerun this script"
  fi
  if command -v apt-get >/dev/null 2>&1; then
    $privilege apt-get update
    $privilege apt-get install -y python3 git tmux curl tar util-linux
  elif command -v dnf >/dev/null 2>&1; then
    $privilege dnf install -y python3 git tmux curl tar util-linux
  elif command -v yum >/dev/null 2>&1; then
    $privilege yum install -y python3 git tmux curl tar util-linux
  else
    die "install the missing packages with the system package manager, then rerun this script"
  fi
fi

codex_is_compatible() {
  command -v codex >/dev/null 2>&1 || return 1
  python3 - "$(codex --version 2>/dev/null || true)" <<'PY'
import re
import sys

match = re.search(r"(\d+)\.(\d+)\.(\d+)", sys.argv[1])
raise SystemExit(0 if match and tuple(map(int, match.groups())) >= (0, 144, 0) else 1)
PY
}

if ! codex_is_compatible; then
  step "Installing Codex CLI (standalone binary, no Node.js needed)"
  mkdir -p "$HOME/.local/bin"
  download_dir="$(mktemp -d)"
  cleanup_download() {
    if [ -n "${download_dir:-}" ] && [ -d "$download_dir" ]; then
      rm -rf -- "$download_dir"
    fi
  }
  trap cleanup_download EXIT
  release_json="$download_dir/release.json"
  archive="$download_dir/codex.tar.gz"
  extract_dir="$download_dir/extracted"
  curl -fsSL https://api.github.com/repos/openai/codex/releases/latest -o "$release_json"
  asset_metadata="$(python3 - "$release_json" "codex-$codex_target.tar.gz" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
asset = next((item for item in data.get("assets", []) if item.get("name") == sys.argv[2]), None)
if asset:
    print(asset.get("browser_download_url", ""))
    print(asset.get("digest", ""))
PY
)"
  asset_url="$(printf '%s\n' "$asset_metadata" | sed -n '1p')"
  asset_digest="$(printf '%s\n' "$asset_metadata" | sed -n '2p')"
  [ -n "$asset_url" ] || die "could not find the official Codex asset for $codex_target"
  case "$asset_digest" in
    sha256:*) ;;
    *) die "the Codex release asset did not include a SHA-256 digest" ;;
  esac
  curl -fsSL "$asset_url" -o "$archive"
  python3 - "$archive" "${asset_digest#sha256:}" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
if digest.hexdigest() != sys.argv[2]:
    raise SystemExit("Codex release checksum mismatch")
PY
  mkdir -p "$extract_dir"
  tar -xzf "$archive" -C "$extract_dir"
  codex_binary="$(find "$extract_dir" -type f \( -name codex -o -name "codex-$codex_target" \) -perm -u+x | head -1)"
  [ -n "$codex_binary" ] || die "Codex archive did not contain a codex binary"
  install -m 755 "$codex_binary" "$HOME/.local/bin/codex"
  export PATH="$HOME/.local/bin:$PATH"
  cleanup_download
  trap - EXIT
  echo "Installed codex to $HOME/.local/bin/codex"
fi
command -v codex >/dev/null 2>&1 || die "codex is not on PATH; add it and rerun"

key_file="$HOME/.config/tele-agent/deepseek.env"
if [ ! -f "$key_file" ]; then
  step "DeepSeek API key"
  deepseek_key="${DEEPSEEK_API_KEY:-}"
  if [ -z "$deepseek_key" ]; then
    read -rsp "Paste the DeepSeek API key (input is hidden): " deepseek_key
    printf '\n'
  else
    echo "DeepSeek API key found in the environment; storing it without printing it."
  fi
  [ -n "$deepseek_key" ] || die "empty DeepSeek key"
  mkdir -p "$(dirname "$key_file")"
  umask 077
  printf 'export DEEPSEEK_API_KEY=%q\n' "$deepseek_key" >"$key_file"
  chmod 600 "$key_file"
  echo "Wrote $key_file (mode 600)"
fi

if [ ! -f "$TELEAGENT_REPO/config/relay.env" ]; then
  cp "$TELEAGENT_REPO/config/relay.env.template" "$TELEAGENT_REPO/config/relay.env"
fi
if ! grep -Eq '^(export[[:space:]]+)?TELEAGENT_DS_KEY_FILE=' "$TELEAGENT_REPO/config/relay.env"; then
  printf 'export TELEAGENT_DS_KEY_FILE="%s"\n' "$key_file" >>"$TELEAGENT_REPO/config/relay.env"
fi
if ! grep -Eq '^(export[[:space:]]+)?TELEAGENT_CODEX_MODEL=' "$TELEAGENT_REPO/config/relay.env"; then
  printf 'export TELEAGENT_CODEX_MODEL="deepseek-v4-flash"\n' >>"$TELEAGENT_REPO/config/relay.env"
fi
if ! grep -Eq '^(export[[:space:]]+)?TELEAGENT_CODEX_REASONING_EFFORT=' "$TELEAGENT_REPO/config/relay.env"; then
  printf 'export TELEAGENT_CODEX_REASONING_EFFORT="max"\n' >>"$TELEAGENT_REPO/config/relay.env"
fi
personality_target="$TELEAGENT_REPO/config/personality.md"
if [[ "${TELEAGENT_INSTANCE:-main}" != "main" ]]; then
  personality_target="$TELEAGENT_REPO/config/personality-${TELEAGENT_INSTANCE}.md"
fi
if [ ! -f "$personality_target" ]; then
  cp "$TELEAGENT_REPO/config/personality.default.md" "$personality_target"
fi

step "Telegram bot setup"
python3 "$SCRIPT_DIR/setup_telegram_notify.py" --repo-root "$TELEAGENT_REPO" --yes

step "Preparing the DeepSeek Codex home"
TELEAGENT_DS_KEY_FILE="$key_file" bash "$SCRIPT_DIR/prepare_telegram_ds_codex_home.sh"

step "Starting the head agent and Telegram inbox"
bash "$SCRIPT_DIR/start_codex_agent.sh"

step "Installing the reboot watchdog"
if command -v crontab >/dev/null 2>&1; then
  printf -v watchdog_q '%q' "$SCRIPT_DIR/ensure_telegram_relay.sh"
  if ! crontab -l 2>/dev/null | grep -q 'ensure_telegram_relay.sh'; then
    (
      crontab -l 2>/dev/null || true
      printf '@reboot %s >/dev/null 2>&1 # tele-agent-relay\n' "$watchdog_q"
      printf '*/1 * * * * %s >/dev/null 2>&1 # tele-agent-relay\n' "$watchdog_q"
    ) | crontab -
    echo "Crontab watchdog installed."
  fi
else
  echo "Crontab is unavailable; the agent is running, but automatic recovery after a reboot was not installed."
fi

step "Sending the setup-complete notification to the owner"
set +e
python3 "$SCRIPT_DIR/notify.py" \
  --title "head agent setup" \
  --message "SETUP COMPLETE — agent online. Hostname: $(hostname)" \
  >/dev/null 2>&1
notify_rc=$?
set -e
if [ "$notify_rc" -eq 0 ]; then
  echo "Owner notified via Telegram."
else
  echo "Telegram notification failed (the owner can still send /ping)."
fi

cat <<EOF

============================================================
SETUP COMPLETE

Tell the owner, who will send these from the bot's Telegram chat:
  /ping
  /status

============================================================
EOF
