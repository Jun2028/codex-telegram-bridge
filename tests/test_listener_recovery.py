from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ListenerRecoveryTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, dict[str, str]]:
        repo = root / "repo with spaces"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        for name in (
            "relay_paths.sh",
            "start_telegram_inbox.sh",
            "telegram_inbox_supervisor.sh",
            "telegram_inbox_watchdog.sh",
            "ensure_telegram_relay.sh",
        ):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        runtime = root / "runtime"
        runtime.mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("TELEAGENT_", "CODEX_"))
        }
        env.update(
            TELEAGENT_REPO=str(repo),
            TELEAGENT_LOG_DIR=str(runtime),
            TELEAGENT_SCRATCH=str(root / "scratch"),
            TELEAGENT_TMUX_SESSION="recovery-fixture",
            TELEAGENT_INBOX_TARGET="recovery-fixture:codex.0",
            TELEAGENT_SECRET_ENV=str(root / "unused.env"),
            TELEAGENT_INBOX_WATCHDOG_INTERVAL="1",
        )
        return repo, runtime, env

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_relay_recovery_does_not_leave_its_lock_in_the_tmux_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, runtime, env = self.fixture(root)
            launcher = repo / "scripts/start_codex_agent.sh"
            launcher.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                set -euo pipefail
                # The caller still owns the lock throughout startup.
                if flock -n "$TELEAGENT_LOG_DIR/telegram_relay.watchdog.lock" true; then
                  echo 'watchdog released its lock before startup completed' >&2
                  exit 1
                fi
                tmux new-session -d -s recovery-fixture -n codex 'sleep 90'
                """))
            launcher.chmod(0o755)
            check = root / "check.sh"
            check.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                set -euo pipefail
                "$TELEAGENT_REPO/scripts/ensure_telegram_relay.sh"
                tmux has-session -t recovery-fixture
                # Subsequent checks must run while the recovered server lives.
                flock -n "$TELEAGENT_LOG_DIR/telegram_relay.watchdog.lock" true
                echo 'recovered tmux is alive; watchdog lock is available'
                """))
            result = subprocess.run(
                [str(ROOT / "scripts/tmux_isolated_test.sh"), "--", "bash", str(check)],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("watchdog lock is available", result.stdout)

    def test_supervisor_recovers_when_log_reopen_and_pid_write_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, runtime, env = self.fixture(Path(directory))
            listener = repo / "faulty_listener.py"
            listener.write_text(textwrap.dedent("""\
                import os, time
                from pathlib import Path
                runtime = Path(os.environ['TELEAGENT_LOG_DIR'])
                counter = runtime / 'attempts'
                attempt = int(counter.read_text()) + 1 if counter.exists() else 1
                counter.write_text(str(attempt))
                if attempt == 1:
                    while not (runtime / 'telegram_inbox.pid').exists():
                        time.sleep(0.02)
                    log = runtime / 'telegram_inbox.supervisor.log'
                    log.rename(runtime / 'original.log')
                    log.mkdir()
                    (runtime / 'telegram_inbox.pid.tmp').mkdir()
                    raise SystemExit(7)
                (runtime / 'recovered.pid').write_text(str(os.getpid()))
                while True:
                    time.sleep(0.1)
                """))
            process = subprocess.Popen(
                [str(repo / "scripts/telegram_inbox_supervisor.sh"), "--", sys.executable, str(listener)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 12
                while not (runtime / "recovered.pid").exists() and time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "supervisor exited during recovery")
                    time.sleep(0.05)
                self.assertTrue((runtime / "recovered.pid").exists(), "listener did not restart")
                self.assertEqual((runtime / "attempts").read_text(), "2")
                self.assertIn("Unable to publish listener PID", (runtime / "original.log").read_text())
                child_pid = int((runtime / "recovered.pid").read_text())
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_watchdog_defers_failed_probes_and_restores_only_missing_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, runtime, env = self.fixture(root)
            (repo / "scripts/telegram_inbox.py").write_text("import time\nwhile True: time.sleep(1)\n")
            queue = runtime / "telegram_relay_queue.state.json"
            queue.write_text('{"tasks": [{"id": "preserved"}]}\n')
            fake_bin = root / "bin"
            fake_bin.mkdir()
            tmux = fake_bin / "tmux"
            tmux.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import os, sys
                from pathlib import Path
                if sys.argv[1:2] == ['list-panes'] and Path(os.environ['PROBE_FAILURE']).exists():
                    raise SystemExit(1)
                os.execv(os.environ['REAL_TMUX'], [os.environ['REAL_TMUX'], *sys.argv[1:]])
                """))
            tmux.chmod(0o755)
            env.update(
                REAL_TMUX=shutil.which("tmux"),
                PROBE_FAILURE=str(root / "probe-failure"),
                PATH=str(fake_bin) + os.pathsep + env["PATH"],
            )
            check = root / "check.py"
            check.write_text(textwrap.dedent("""\
                import os, shlex, subprocess, time
                from pathlib import Path
                repo = Path(os.environ['TELEAGENT_REPO'])
                runtime = Path(os.environ['TELEAGENT_LOG_DIR'])
                def run(*args):
                    return subprocess.check_output(args, text=True).strip()
                run('tmux', 'new-session', '-d', '-s', 'recovery-fixture', '-n', 'codex', 'sleep 90')
                before = run('tmux', 'display-message', '-p', '-t', 'recovery-fixture:codex.0', '#{pane_id}:#{pane_pid}')
                launcher = str(repo / 'scripts/start_telegram_inbox.sh')
                launches = [subprocess.Popen(
                    [launcher, '--session', 'recovery-fixture', '--target-pane', 'recovery-fixture:codex.0'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ) for _ in range(2)]
                try:
                    for process in launches:
                        # The launcher allows 15 seconds for listener startup.
                        process.communicate(timeout=20)
                finally:
                    for process in launches:
                        if process.poll() is None:
                            process.terminate()
                            process.communicate(timeout=5)
                assert sorted(process.returncode for process in launches) == [0, 2]
                windows = run('tmux', 'list-windows', '-t', 'recovery-fixture', '-F', '#{window_name}').splitlines()
                assert windows.count('inbox') == 1, windows
                previous = (runtime / 'telegram_inbox.pid').read_text()
                watchdog = str(repo / 'scripts/telegram_inbox_watchdog.sh')
                run('tmux', 'new-window', '-t', 'recovery-fixture', '-n', 'inbox-guard', 'exec ' + shlex.join(['bash', watchdog]))
                deadline = time.monotonic() + 5
                while not (runtime / 'telegram_inbox.watchdog.log').exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                # A second guard must exit without duplicating recovery.
                subprocess.run([watchdog], check=True, timeout=3)
                failure = Path(os.environ['PROBE_FAILURE'])
                failure.touch()
                run('tmux', 'kill-window', '-t', 'recovery-fixture:inbox')
                time.sleep(3)
                windows = run('tmux', 'list-windows', '-t', 'recovery-fixture', '-F', '#{window_name}').splitlines()
                assert 'inbox' not in windows, windows
                failure.unlink()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    pid_file = runtime / 'telegram_inbox.pid'
                    # The supervisor publishes its PID before the launcher
                    # finishes. Killing tmux then can leave that launcher
                    # holding open fixture files during NFS cleanup.
                    started = 'Listener started' in (runtime / 'telegram_inbox.watchdog.log').read_text()
                    if started and pid_file.exists() and pid_file.read_text() != previous:
                        pid = int(pid_file.read_text())
                        os.kill(pid, 0)
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError('watchdog did not restore listener')
                after = run('tmux', 'display-message', '-p', '-t', 'recovery-fixture:codex.0', '#{pane_id}:#{pane_pid}')
                assert before == after, (before, after)
                print('listener restored; agent pane preserved')
                """))
            result = subprocess.run(
                [str(ROOT / "scripts/tmux_isolated_test.sh"), "--", sys.executable, str(check)],
                env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("listener restored; agent pane preserved", result.stdout)
            self.assertEqual(queue.read_text(), '{"tasks": [{"id": "preserved"}]}\n')


if __name__ == "__main__":
    unittest.main()
