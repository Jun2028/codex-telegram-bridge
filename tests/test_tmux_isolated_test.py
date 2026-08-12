from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "tmux_isolated_test.sh"


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TmuxIsolationTests(unittest.TestCase):
    def test_inherited_tmux_cannot_be_killed(self) -> None:
        outer_root = tempfile.TemporaryDirectory()
        socket = "outer-" + uuid.uuid4().hex
        done = Path(outer_root.name) / "done"
        command = (
            f"{HELPER} -- bash -c 'tmux new-session -d -s fixture sleep\\ 30; "
            f"tmux kill-server'; touch {done}; sleep 30"
        )
        env = {"TMUX_TMPDIR": outer_root.name}
        try:
            subprocess.run(
                ["env", "-u", "TMUX", "tmux", "-L", socket,
                 "new-session", "-d", "-s", "outer", command],
                env=env, check=True,
            )
            for _ in range(50):
                if done.exists():
                    break
                time.sleep(0.1)
            self.assertTrue(done.exists())
            result = subprocess.run(
                ["env", "-u", "TMUX", "tmux", "-L", socket,
                 "has-session", "-t", "outer"], env=env, check=False,
            )
            self.assertEqual(result.returncode, 0)
        finally:
            subprocess.run(
                ["env", "-u", "TMUX", "tmux", "-L", socket, "kill-server"],
                env=env, check=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            outer_root.cleanup()


if __name__ == "__main__":
    unittest.main()
