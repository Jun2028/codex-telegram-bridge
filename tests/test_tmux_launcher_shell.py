from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
ISOLATED_TMUX = REPO_ROOT / "scripts" / "tmux_isolated_test.sh"
START_AGENT = REPO_ROOT / "scripts" / "start_codex_agent.sh"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import telegram_inbox  # noqa: E402


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TmuxLauncherShellTests(unittest.TestCase):
    def test_non_main_instance_rejects_shared_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_repo = root / "repo"
            fixture_config = fixture_repo / "config"
            fixture_config.mkdir(parents=True)
            shared_home = root / "shared-codex-home"
            (fixture_config / "relay.env").write_text(
                f"TELEAGENT_CODEX_HOME={shared_home}\n", encoding="utf-8"
            )
            (fixture_config / "relay-beta.env").write_text(
                f"TELEAGENT_CODEX_HOME={shared_home}\n", encoding="utf-8"
            )
            env = os.environ.copy()
            env.update(
                {
                    "TELEAGENT_INSTANCE": "beta",
                    "TELEAGENT_REPO": str(fixture_repo),
                }
            )

            result = subprocess.run(
                ["bash", "-c", f"source {REPO_ROOT / 'scripts' / 'relay_paths.sh'}"],
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must use a private TELEAGENT_CODEX_HOME", result.stdout)

    def test_two_non_main_instances_cannot_claim_the_same_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_repo = root / "repo"
            fixture_config = fixture_repo / "config"
            fixture_config.mkdir(parents=True)
            source_home = root / "main-codex-home"
            shared_private_home = root / "shared-private-codex-home"
            (fixture_config / "relay.env").write_text(
                f"TELEAGENT_CODEX_HOME={source_home}\n", encoding="utf-8"
            )
            for instance in ("beta", "gamma"):
                (fixture_config / f"relay-{instance}.env").write_text(
                    f"TELEAGENT_CODEX_HOME={shared_private_home}\n",
                    encoding="utf-8",
                )
            command = (
                f"source {REPO_ROOT / 'scripts' / 'relay_paths.sh'} && "
                'tele_agent_claim_codex_home "$TELEAGENT_CODEX_HOME"'
            )
            base_env = os.environ.copy()
            base_env["TELEAGENT_REPO"] = str(fixture_repo)
            beta_env = base_env.copy()
            beta_env["TELEAGENT_INSTANCE"] = "beta"
            gamma_env = base_env.copy()
            gamma_env["TELEAGENT_INSTANCE"] = "gamma"

            first = subprocess.run(
                ["bash", "-c", command],
                env=beta_env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            second = subprocess.run(
                ["bash", "-c", command],
                env=gamma_env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )

            self.assertEqual(first.returncode, 0, first.stdout)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("belongs to tele-agent instance 'beta'", second.stdout)

    def test_stack_starts_when_tmux_inherits_posix_sh(self) -> None:
        live_session_existed = (
            subprocess.run(
                ["tmux", "has-session", "-t", "tele-agent"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_repo = root / "repo with spaces"
            fixture_scripts = fixture_repo / "scripts"
            fake_bin = root / "bin"
            runtime = root / "runtime"
            fixture_scripts.mkdir(parents=True)
            fake_bin.mkdir()

            os.symlink(
                REPO_ROOT / "scripts" / "relay_paths.sh",
                fixture_scripts / "relay_paths.sh",
            )
            (fixture_scripts / "telegram_inbox.py").write_text(
                "import time\nwhile True:\n    time.sleep(1)\n",
                encoding="utf-8",
            )
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import ctypes
                    import os
                    from pathlib import Path
                    import sys
                    import time

                    Path(os.environ["FAKE_CODEX_ARGS_PATH"]).write_text(
                        "\\n".join(sys.argv[1:]), encoding="utf-8"
                    )
                    Path(os.environ["FAKE_CODEX_ENV_PATH"]).write_text(
                        "\\n".join(
                            f"{key}={os.environ.get(key, '')}"
                            for key in (
                                "CODEX_HOME",
                                "TELEAGENT_AGENT_ID",
                                "TELEAGENT_AGENT_JSONL",
                                "TELEAGENT_AGENT_META",
                                "TELEAGENT_AGENT_OUTBOX",
                                "TELEAGENT_AGENT_TARGET_PANE",
                            )
                        ),
                        encoding="utf-8",
                    )
                    ctypes.CDLL(None).prctl(15, b"codex", 0, 0, 0)
                    while True:
                        time.sleep(1)
                    """
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            check_script = root / "check.sh"
            check_script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    bash {START_AGENT} --session shell-fixture
                    test "$(tmux show-options -gv default-shell)" = /bin/sh
                    test "$(tmux display-message -p -t shell-fixture:sh.0 '#{{pane_current_command}}')" = bash
                    pane_pid="$(tmux display-message -p -t shell-fixture:codex.0 '#{{pane_pid}}')"
                    supervisor_pid="$(ps -o pid=,args= --ppid "$pane_pid" | awk '/codex_agent_supervisor[.]sh/{{print $1; exit}}')"
                    test -n "$supervisor_pid"
                    ps -o comm= --ppid "$supervisor_pid" | grep -Fx codex
                    grep -Fx check_for_update_on_startup=false "$FAKE_CODEX_ARGS_PATH"
                    grep -Eq '^TELEAGENT_AGENT_ID=codex-' "$FAKE_CODEX_ENV_PATH"
                    grep -Fx 'TELEAGENT_AGENT_TARGET_PANE=shell-fixture:codex.0' "$FAKE_CODEX_ENV_PATH"
                    inbox_pid="$(<"$TELEAGENT_LOG_DIR/telegram_inbox.pid")"
                    kill -0 "$inbox_pid"
                    grep -aFq scripts/telegram_inbox.py "/proc/$inbox_pid/cmdline"
                    """
                ),
                encoding="utf-8",
            )
            check_script.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "SHELL": "/bin/sh",
                    "TELEAGENT_REPO": str(fixture_repo),
                    "TELEAGENT_SCRATCH": str(root / "scratch"),
                    "TELEAGENT_LOG_DIR": str(runtime),
                    "TELEAGENT_SECRET_ENV": str(root / "unused-secret.env"),
                    "TELEAGENT_CODEX_HOME": str(root / "codex-home"),
                    "FAKE_CODEX_ARGS_PATH": str(root / "codex-args.txt"),
                    "FAKE_CODEX_ENV_PATH": str(root / "codex-env.txt"),
                    "TELEAGENT_CODEX_MODEL": "gpt-5.6-sol",
                    "TELEAGENT_CODEX_REASONING_EFFORT": "max",
                }
            )
            result = subprocess.run(
                [str(ISOLATED_TMUX), "--", str(check_script)],
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout)

        if live_session_existed:
            self.assertEqual(
                subprocess.run(
                    ["tmux", "has-session", "-t", "tele-agent"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode,
                0,
                "isolated launcher test disturbed the live tele-agent session",
            )

    def test_non_main_agent_keeps_its_binding_and_private_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_repo = root / "repo with spaces"
            fixture_scripts = fixture_repo / "scripts"
            fixture_config = fixture_repo / "config"
            fake_bin = root / "bin"
            runtime = root / "beta-runtime"
            scratch = root / "beta-scratch"
            source_home = root / "main-codex-home"
            private_home = scratch / "codex-home"
            fixture_scripts.mkdir(parents=True)
            fixture_config.mkdir()
            fake_bin.mkdir()
            source_home.mkdir()

            os.symlink(
                REPO_ROOT / "scripts" / "relay_paths.sh",
                fixture_scripts / "relay_paths.sh",
            )
            (fixture_scripts / "telegram_inbox.py").write_text(
                "import time\nwhile True:\n    time.sleep(1)\n",
                encoding="utf-8",
            )
            (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
            (source_home / "config.toml").write_text(
                f'codex_home = "{source_home}"\n', encoding="utf-8"
            )
            source_sessions = source_home / "sessions"
            source_sessions.mkdir()
            (source_sessions / "must-not-copy.jsonl").write_text(
                "source chat\n", encoding="utf-8"
            )
            (fixture_config / "relay.env").write_text(
                f'TELEAGENT_CODEX_HOME="${{TELEAGENT_CODEX_HOME:-{source_home}}}"\n',
                encoding="utf-8",
            )
            (fixture_config / "relay-beta.env").write_text(
                "\n".join(
                    (
                        f"TELEAGENT_CODEX_HOME={private_home}",
                        f"TELEAGENT_INSTANCE_SCRATCH={scratch}",
                        f"TELEAGENT_INSTANCE_LOG_DIR={runtime}",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import ctypes
                    import os
                    from pathlib import Path
                    import time

                    Path(os.environ["FAKE_CODEX_ENV_PATH"]).write_text(
                        "\\n".join(
                            f"{key}={os.environ.get(key, '')}"
                            for key in (
                                "CODEX_HOME",
                                "TELEAGENT_AGENT_ID",
                                "TELEAGENT_AGENT_JSONL",
                                "TELEAGENT_AGENT_META",
                                "TELEAGENT_AGENT_OUTBOX",
                                "TELEAGENT_AGENT_TARGET_PANE",
                            )
                        ),
                        encoding="utf-8",
                    )
                    ctypes.CDLL(None).prctl(15, b"codex", 0, 0, 0)
                    while True:
                        time.sleep(1)
                    """
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            check_script = root / "check-beta.sh"
            check_script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    TELEAGENT_INSTANCE=beta bash {START_AGENT} --session beta-shell-fixture
                    grep -Fx 'CODEX_HOME={private_home}' "$FAKE_CODEX_ENV_PATH"
                    grep -Eq '^TELEAGENT_AGENT_ID=codex-' "$FAKE_CODEX_ENV_PATH"
                    grep -Fqx 'TELEAGENT_AGENT_TARGET_PANE=beta-shell-fixture:codex.0' "$FAKE_CODEX_ENV_PATH"
                    grep -Fq '"codex_home": "{private_home}"' '{runtime}'/agents/*/meta.json
                    grep -Fx beta '{private_home}/.tele-agent-instance'
                    test -L '{private_home}/auth.json'
                    test "$(readlink -f '{private_home}/auth.json')" = '{source_home}/auth.json'
                    grep -Fq '{private_home}' '{private_home}/config.toml'
                    ! grep -Fq '{source_home}' '{private_home}/config.toml'
                    test ! -e '{private_home}/sessions/must-not-copy.jsonl'
                    test ! -L '{private_home}/sessions'
                    """
                ),
                encoding="utf-8",
            )
            check_script.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "SHELL": "/bin/sh",
                    "TELEAGENT_REPO": str(fixture_repo),
                    "TELEAGENT_SECRET_ENV": str(root / "unused-secret.env"),
                    "FAKE_CODEX_ARGS_PATH": str(root / "codex-args.txt"),
                    "FAKE_CODEX_ENV_PATH": str(root / "codex-env.txt"),
                }
            )
            result = subprocess.run(
                [str(ISOLATED_TMUX), "--", str(check_script)],
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout)


class PythonLifecycleShellTests(unittest.TestCase):
    def test_restart_route_uses_bash_and_records_configured_home(self) -> None:
        repo_root = Path("/tmp/tele-agent-python-route")
        private_home = Path("/tmp/tele-agent-python-route-home")
        meta = {
            "agent_id": "agent-test",
            "agent_jsonl": "/tmp/agent/events.jsonl",
            "meta_json": "/tmp/agent/meta.json",
            "outbox_path": "/tmp/outbox.jsonl",
            "target_pane": "beta-python:codex.0",
        }
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TELEAGENT_INSTANCE": "beta",
                    "TELEAGENT_CODEX_HOME": str(private_home),
                },
            ),
            mock.patch.object(telegram_inbox, "ensure_tmux_session"),
            mock.patch.object(
                telegram_inbox, "tmux_window_exists", return_value=False
            ),
            mock.patch.object(telegram_inbox.subprocess, "run") as run,
            mock.patch.object(
                telegram_inbox.agent_registry,
                "create_agent",
                return_value=meta,
            ) as create_agent,
            mock.patch.object(
                telegram_inbox.agent_registry,
                "shell_env_prefix",
                return_value="TELEAGENT_PRESERVE_AGENT_BINDING=1",
            ),
            mock.patch.object(
                telegram_inbox.agent_registry,
                "refresh_codex_session_link",
                return_value=meta,
            ),
            mock.patch.object(
                telegram_inbox, "codex_executable", return_value="/tmp/fake-codex"
            ),
            mock.patch.object(
                telegram_inbox, "tmux_pane_has_codex_process", return_value=True
            ),
        ):
            telegram_inbox.start_codex_agent(
                repo_root=repo_root,
                session="beta-python",
                window="codex",
                restart=True,
            )

        new_window = next(
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:2] == ["tmux", "new-window"]
        )
        self.assertIn("--noprofile", new_window[-1])
        self.assertIn("--norc", new_window[-1])
        self.assertTrue(new_window[-1].startswith("exec "))
        send_keys = next(
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:2] == ["tmux", "send-keys"]
        )
        self.assertIn("export TELEAGENT_INSTANCE=beta", send_keys[-2])
        self.assertIn("TELEAGENT_PRESERVE_AGENT_BINDING=1", send_keys[-2])
        create_agent.assert_called_once_with(
            repo_root=repo_root,
            session="beta-python",
            window="codex",
            target_pane="beta-python:codex.0",
            launch_source="telegram-restart-agent",
            start_epoch=mock.ANY,
            codex_home=private_home.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
