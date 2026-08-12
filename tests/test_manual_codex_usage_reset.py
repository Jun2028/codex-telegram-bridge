from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "manual_codex_usage_reset.sh"


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class ManualCodexUsageResetTests(unittest.TestCase):
    def setUp(self) -> None:
        # The production helper intentionally rejects runtime state outside
        # the user's home. Keep the fixture inside that same safety boundary.
        self.temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "state").mkdir(parents=True)
        self.session = "test-codex-reset-" + uuid.uuid4().hex
        self.watchdog = self.root / "fake_watchdog.sh"
        self.watchdog.write_text(
            """#!/usr/bin/env bash
set -eu
if [ "${1-}" = "--inspect-usage" ]; then
  printf '2026-07-30T00:00:00Z\\t66.0\\t1785902940\\n'
  exit 0
fi
printf 'historical watchdog output\\n'
while :; do sleep 1; done
""",
            encoding="utf-8",
        )
        self.watchdog.chmod(0o700)

    def tearDown(self) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", self.session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.temporary.cleanup()

    def env(self) -> dict[str, str]:
        values = os.environ.copy()
        values.update(
            {
                "TELEAGENT_CODEX_RESET_RUNTIME": str(self.runtime),
                "TELEAGENT_CODEX_RESET_WATCHDOG_SCRIPT": str(self.watchdog),
                "TELEAGENT_CODEX_RESET_WATCHDOG_SESSION": self.session,
            }
        )
        return values

    def start_session(self, command: str) -> None:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session,
                "-c",
                str(self.runtime),
                command,
            ],
            check=True,
        )
        time.sleep(0.2)

    def test_historical_pane_output_does_not_look_interactive(self) -> None:
        self.start_session(f"exec {shlex_quote(str(self.watchdog))}")

        result = subprocess.run(
            [str(HELPER), "--inspect"],
            env=self.env(),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\t66.0\t", result.stdout)
        self.assertNotIn("unexpectedly interactive", result.stderr)
        self.assertEqual(
            subprocess.run(
                ["tmux", "has-session", "-t", self.session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode,
            0,
        )

    def test_unexpected_process_is_not_replaced(self) -> None:
        self.start_session("exec sleep 300")

        result = subprocess.run(
            [str(HELPER), "--inspect"],
            env=self.env(),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 6)
        self.assertIn("does not own the expected watchdog process", result.stderr)
        self.assertEqual(
            subprocess.run(
                ["tmux", "has-session", "-t", self.session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode,
            0,
        )


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    unittest.main()
