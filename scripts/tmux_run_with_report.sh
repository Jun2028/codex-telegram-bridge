#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/tmux_run_with_report.sh --title TITLE [--session tele-agent] [--] COMMAND...

Runs COMMAND, logs stdout/stderr under logs/readable/auto_reports/, and sends
Telegram/email notifications at start and finish/failure using scripts/notify.py.
Use this inside tmux or scheduled compute allocations.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/relay_paths.sh"

TITLE=""
SESSION="${TELEAGENT_TMUX_SESSION:-tele-agent}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --title)
      TITLE="${2:-}"
      shift 2
      ;;
    --session)
      SESSION="${2:-}"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

if [[ -z "$TITLE" || $# -eq 0 ]]; then
  usage
  exit 2
fi

mkdir -p "$TELEAGENT_REPO/logs/readable/auto_reports"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SAFE_TITLE="$(printf '%s' "$TITLE" | tr -cs 'A-Za-z0-9_.-' '_' | sed 's/^_//;s/_$//')"
LOG_PATH="$TELEAGENT_REPO/logs/readable/auto_reports/${STAMP}_${SAFE_TITLE:-job}.log"
SUMMARY_PATH="${LOG_PATH%.log}.summary.txt"
export TELEAGENT_RUN_LOG_PATH="$LOG_PATH"
export TELEAGENT_RUN_SUMMARY_PATH="$SUMMARY_PATH"
export TELEAGENT_RUN_TITLE="$TITLE"

format_duration() {
  local seconds="$1"
  local minutes=$((seconds / 60))
  local remain=$((seconds % 60))
  if [[ "$minutes" -gt 0 ]]; then
    printf '%dm%02ds' "$minutes" "$remain"
  else
    printf '%ds' "$seconds"
  fi
}

pbs_compute_summary() {
  if [[ -z "${PBS_JOBID:-}" ]]; then
    printf 'requested_compute: local/unscheduled\n'
    return
  fi
  python3 - "${PBS_JOBID:-}" "${PBS_QUEUE:-}" <<'PY'
import re
import subprocess
import sys

jobid, queue_env = sys.argv[1], sys.argv[2]
queue = queue_env or "unknown"
ngpus = "unknown"
ncpus = "unknown"
walltime = "unknown"
try:
    output = subprocess.check_output(["qstat", "-f", jobid], text=True, stderr=subprocess.DEVNULL, timeout=5)
except Exception:
    output = ""
if output:
    joined = re.sub(r"\n\t", "", output)
    def field(name: str):
        match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.+)$", joined, re.MULTILINE)
        return match.group(1).strip() if match else None
    queue = field("queue") or queue
    ngpus = field("Resource_List.ngpus") or ngpus
    ncpus = field("Resource_List.ncpus") or ncpus
    walltime = field("Resource_List.walltime") or walltime
print(f"requested_compute: queue={queue}; GPUs={ngpus}; CPU cores={ncpus}; walltime={walltime}")
PY
}

summarize_result() {
  local log_path="$1"
  local summary_path="$2"
  local status="$3"
  python3 - "$log_path" "$summary_path" "$status" <<'PY'
import re
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
status = int(sys.argv[3])

ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
path_like = re.compile(r"(?<!\w)/(?:scratch|nfs|home|Users|var|tmp)[^\s,;)]*")

def clean(line: str) -> str:
    line = ansi.sub("", line).replace("\r", "\n")
    line = path_like.sub("[local artifact]", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line[:220]

def emit(lines: list[str]) -> None:
    seen = set()
    out = []
    for line in lines:
        line = clean(line)
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= 5:
            break
    for line in out:
        print(f"- {line}")

if summary_path.exists() and summary_path.stat().st_size:
    emit(summary_path.read_text(encoding="utf-8", errors="replace").splitlines())
    raise SystemExit

if not log_path.exists():
    fallback = "Completed, but no local summary file was produced." if status == 0 else "Failed before a local log was written."
    print(f"- {fallback}")
    raise SystemExit

# Process exit is the only result the wrapper can verify. A reviewed summary
# file can describe experiment outcomes; arbitrary matching log lines cannot.
if status == 0:
    print("- Process exited successfully. No reviewed result summary was supplied.")
else:
    print("- Process failed. Inspect the local log for the cause.")

PY
}

START_MESSAGE=$(cat <<EOF
status: started
host: $(hostname)
pbs_jobid: ${PBS_JOBID:-}
pbs_queue: ${PBS_QUEUE:-}
$(pbs_compute_summary)
summary:
- Started. I will send a concise result summary when it finishes.
EOF
)

python3 "$TELEAGENT_REPO/scripts/notify.py" \
  --repo-root "$TELEAGENT_REPO" \
  --title "$TITLE started" \
  --level info \
  --message "$START_MESSAGE" || true

tele_agent_log "reported command started: $TITLE"
START_TS="$(date +%s)"
set +e
"$@" >"$LOG_PATH" 2>&1
STATUS=$?
set -e
END_TS="$(date +%s)"
RUNTIME="$(format_duration "$((END_TS - START_TS))")"
RESULT_SUMMARY="$(summarize_result "$LOG_PATH" "$SUMMARY_PATH" "$STATUS")"

END_MESSAGE=$(cat <<EOF
status: $([[ "$STATUS" -eq 0 ]] && printf completed || printf failed)
exit_status: $STATUS
runtime: $RUNTIME
finished_at: $(date -Iseconds)
host: $(hostname)
pbs_jobid: ${PBS_JOBID:-}
pbs_queue: ${PBS_QUEUE:-}
$(pbs_compute_summary)
summary:
$RESULT_SUMMARY
EOF
)

if [[ "$STATUS" -eq 0 ]]; then
  LEVEL="success"
  END_TITLE="$TITLE completed"
else
  LEVEL="error"
  END_TITLE="$TITLE failed"
fi

python3 "$TELEAGENT_REPO/scripts/notify.py" \
  --repo-root "$TELEAGENT_REPO" \
  --title "$END_TITLE" \
  --level "$LEVEL" \
  --message "$END_MESSAGE" || true

tele_agent_log "reported command finished: $TITLE status=$STATUS log=$LOG_PATH"
exit "$STATUS"
