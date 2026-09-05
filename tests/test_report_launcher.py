from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReportLauncherTests(unittest.TestCase):
    def test_listener_deployment_preserves_agent_and_existing_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            scripts = repo / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "relay_paths.sh").symlink_to(ROOT / "scripts/relay_paths.sh")
            (scripts / "telegram_inbox.py").write_text(
                textwrap.dedent("""\
                import json, os, subprocess, time
                from pathlib import Path
                runtime = Path(os.environ['TELEAGENT_LOG_DIR'])
                revision = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], text=True).strip()
                while True:
                    (runtime / 'telegram_health.state.json').write_text(json.dumps({'revision': revision, 'delivery_ok_ts': time.time()}))
                    time.sleep(0.1)
                """)
            )
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "scripts"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            runtime = root / "runtime"
            runtime.mkdir()
            queue_file = runtime / "telegram_relay_queue.state.json"
            queue_file.write_text('{"tasks": [{"id": "untouched"}]}\n')
            check = root / "deploy-check.sh"
            check.write_text(
                textwrap.dedent("""\
                #!/bin/bash
                set -euo pipefail
                tmux new-session -d -s deploy-fixture -n codex 'sleep 60'
                "$TEST_RELAY_SOURCE/scripts/start_telegram_inbox.sh" --session deploy-fixture --target-pane deploy-fixture:codex.0
                "$TEST_RELAY_SOURCE/scripts/deploy_listener.sh"
                """)
            )
            check.chmod(0o755)
            environment = {
                **os.environ,
                "TELEAGENT_REPO": str(repo),
                "TELEAGENT_SCRATCH": str(root / "scratch"),
                "TELEAGENT_LOG_DIR": str(runtime),
                "TELEAGENT_TMUX_SESSION": "deploy-fixture",
                "TELEAGENT_INBOX_TARGET": "deploy-fixture:codex.0",
                "TEST_RELAY_SOURCE": str(ROOT),
                "TELEAGENT_SECRET_ENV": str(root / "unused.env"),
            }
            result = subprocess.run(
                [str(ROOT / "scripts/tmux_isolated_test.sh"), "--", str(check)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("agent pane preserved", result.stdout)
            self.assertEqual(
                queue_file.read_text(), '{"tasks": [{"id": "untouched"}]}\n'
            )

    def test_job_uses_a_new_window_and_keeps_the_selected_pane_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            scripts = repo / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "relay_paths.sh").symlink_to(ROOT / "scripts/relay_paths.sh")
            runner = scripts / "tmux_run_with_report.sh"
            runner.write_text(
                '#!/bin/bash\nmkdir -p "$TELEAGENT_SCRATCH"\nprintf "started" > "$TELEAGENT_SCRATCH/result"\nsleep 30\n'
            )
            runner.chmod(0o755)
            check = root / "check.sh"
            check.write_text(
                textwrap.dedent("""\
                #!/bin/bash
                set -euo pipefail
                trap 'printf "fixture failed at line %s\\n" "$LINENO" >&2' ERR
                tmux new-session -d -s report-fixture -n agent 'sleep 30'
                before=$(tmux display-message -p -t report-fixture:agent.0 '#{pane_id}:#{pane_pid}')
                "$TEST_RELAY_SOURCE/scripts/tmux_send_reported.sh" --session report-fixture --title 'fixture job' 'printf safe'
                for attempt in {1..200}; do
                  [[ -f "$TELEAGENT_SCRATCH/result" ]] && break
                  sleep 0.05
                done
                test -f "$TELEAGENT_SCRATCH/result"
                after=$(tmux display-message -p -t report-fixture:agent.0 '#{pane_id}:#{pane_pid}')
                test "$before" = "$after"
                test -z "$(tmux capture-pane -p -t report-fixture:agent.0 | tr -d '[:space:]')"
                tmux list-windows -t report-fixture -F '#{window_name}' | grep -q '^job-fixture-job-'
                """)
            )
            check.chmod(0o755)
            environment = {
                **os.environ,
                "TELEAGENT_REPO": str(repo),
                "TELEAGENT_SCRATCH": str(root / "scratch"),
                "TELEAGENT_LOG_DIR": str(root / "runtime"),
                "TEST_RELAY_SOURCE": str(ROOT),
            }
            result = subprocess.run(
                [str(ROOT / "scripts/tmux_isolated_test.sh"), "--", str(check)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=25,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
