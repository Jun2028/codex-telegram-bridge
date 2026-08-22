#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/telegram_agent_reply.sh ack|progress|final MESSAGE...

Legacy compatibility helper. Normal Codex agent_message events are now
forwarded automatically; new agents should not call this script.
EOF
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

if [[ -z "${TELEAGENT_AGENT_ID:-}" || -z "${TELEAGENT_AGENT_JSONL:-}" ]]; then
  DEFAULT_AGENT_TARGET="${TELEAGENT_AGENT_TARGET_PANE:-${TELEAGENT_INBOX_TARGET:-${TELEAGENT_TMUX_SESSION:-tele-agent}:${TELEAGENT_CODEX_WINDOW:-codex}.0}}"
  if AGENT_EXPORTS="$(python3 "$SCRIPT_DIR/telegram_agent_registry.py" current --target-pane "$DEFAULT_AGENT_TARGET" --refresh --shell-exports 2>/dev/null)"; then
    eval "$AGENT_EXPORTS"
  fi
fi

KIND="$1"
shift
MESSAGE="$*"

case "$KIND" in
  ack)
    TITLE="Codex ack"
    LEVEL="info"
    PREFIX=""
    SUFFIX=""
    ;;
  progress)
    TITLE="Codex progress"
    LEVEL="info"
    PREFIX=""
    SUFFIX=""
    ;;
  final)
    TITLE="Codex final"
    LEVEL="success"
    PREFIX=""
    SUFFIX=" ∎"
    ;;
  *)
    usage
    exit 2
    ;;
esac

OUTBOX="${TELEAGENT_AGENT_OUTBOX:-$TELEAGENT_LOG_DIR/telegram_agent_outbox.jsonl}"
mkdir -p "$(dirname "$OUTBOX")"

PHASE="$KIND" TITLE="$TITLE" LEVEL="$LEVEL" PREFIX="$PREFIX" SUFFIX="$SUFFIX" MESSAGE="$MESSAGE" OUTBOX="$OUTBOX" AGENT_ID="${TELEAGENT_AGENT_ID:-}" AGENT_JSONL="${TELEAGENT_AGENT_JSONL:-}" AGENT_META="${TELEAGENT_AGENT_META:-}" python3 - <<'PY'
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["OUTBOX"])
now = int(time.time())
record = {
    "ts": now,
    "ts_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
    "agent_id": os.environ.get("AGENT_ID") or None,
    "agent_jsonl": os.environ.get("AGENT_JSONL") or None,
    "agent_meta": os.environ.get("AGENT_META") or None,
    "phase": os.environ["PHASE"],
    "title": os.environ["TITLE"],
    "level": os.environ["LEVEL"],
    "text": os.environ["PREFIX"] + os.environ["MESSAGE"] + os.environ["SUFFIX"],
}
record = {key: value for key, value in record.items() if value is not None}
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\n")
agent_jsonl = os.environ.get("AGENT_JSONL", "").strip()
if agent_jsonl:
    agent_record = dict(record)
    agent_record["event"] = "agent_reply_queued"
    agent_path = Path(agent_jsonl)
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    with agent_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(agent_record, sort_keys=True) + "\n")
print(f"queued Telegram {record['phase']} reply")
PY
