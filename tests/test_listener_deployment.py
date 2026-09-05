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


if __name__ == "__main__":
    unittest.main()
