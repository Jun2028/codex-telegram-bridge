from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import textwrap
import time
import tomllib
import types
import unittest
from datetime import datetime, timezone


REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_CONFIG = REPO_ROOT / "scripts" / "render_chat_only_codex_config.py"
PREPARE_HOME = REPO_ROOT / "scripts" / "prepare_telegram_codex_home.sh"
RELAY_PATHS = REPO_ROOT / "scripts" / "relay_paths.sh"
SUPERVISOR = REPO_ROOT / "scripts" / "codex_agent_supervisor.sh"


class ChatOnlyConfigTests(unittest.TestCase):
    def test_renderer_embeds_personality_and_denies_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personality = root / "personality.md"
            output = root / "codex-home" / "config.toml"
            personality.write_text(
                "You are TestBot. 中 English awkwardly ✨\n", encoding="utf-8"
            )

            subprocess.run(
                [
                    str(RENDER_CONFIG),
                    "--output",
                    str(output),
                    "--personality",
                    str(personality),
                ],
                check=True,
                timeout=10,
            )

            config = tomllib.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(config["approval_policy"], "never")
            self.assertEqual(config["default_permissions"], "tele-agent-chat-only")
            self.assertEqual(config["web_search"], "disabled")
            self.assertIn("You are TestBot", config["developer_instructions"])
            self.assertIn("中 English awkwardly ✨", config["developer_instructions"])
            profile = config["permissions"]["tele-agent-chat-only"]
            self.assertEqual(profile["filesystem"], {":root": "deny"})
            self.assertEqual(profile["network"], {"enabled": False})
            self.assertTrue(config["features"])
            self.assertFalse(any(config["features"].values()))
            self.assertEqual(config["shell_environment_policy"]["inherit"], "none")
            self.assertNotIn("mcp_servers", config)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_deepseek_renderer_keeps_provider_but_not_tool_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personality = root / "personality.md"
            output = root / "config.toml"
            catalog = root / "models.json"
            model_spec = root / "agent-model.json"
            personality.write_text("A conversational bot.\n", encoding="utf-8")

            subprocess.run(
                [
                    str(RENDER_CONFIG),
                    "--output",
                    str(output),
                    "--personality",
                    str(personality),
                    "--provider",
                    "deepseek",
                    "--model-catalog",
                    str(catalog),
                    "--model-spec",
                    str(model_spec),
                ],
                check=True,
                timeout=10,
            )

            config = tomllib.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(config["model_provider"], "deepseek")
            self.assertEqual(config["model_catalog_json"], str(catalog))
            self.assertEqual(
                config["model_providers"]["deepseek"]["env_key"],
                "DEEPSEEK_API_KEY",
            )
            self.assertEqual(
                config["shell_environment_policy"]["set"][
                    "AGENT_MODEL_SPEC_PATH"
                ],
                str(model_spec),
            )
            self.assertEqual(
                config["permissions"]["tele-agent-chat-only"]["filesystem"],
                {":root": "deny"},
            )

    def test_main_access_mode_is_not_inherited_by_another_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_repo = root / "repo"
            fixture_config = fixture_repo / "config"
            fixture_config.mkdir(parents=True)
            (fixture_config / "relay.env").write_text(
                "\n".join(
                    (
                        'TELEAGENT_CODEX_ACCESS_MODE="chat-only"',
                        f'TELEAGENT_CODEX_HOME="{root / "main-home"}"',
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "TELEAGENT_INSTANCE": "beta",
                    "TELEAGENT_REPO": str(fixture_repo),
                }
            )

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {RELAY_PATHS} && printf '%s' \"$TELEAGENT_CODEX_ACCESS_MODE\"",
                ],
                env=env,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )

            self.assertEqual(result.stdout, "full-access")

    def test_prepare_uses_clean_home_and_empty_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_repo = root / "repo"
            fixture_config = fixture_repo / "config"
            fixture_config.mkdir(parents=True)
            source_home = root / "main-codex-home"
            scratch = root / "beta-scratch"
            source_home.mkdir()
            (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
            (source_home / "plugins").mkdir()
            (fixture_config / "personality-beta.md").write_text(
                "You are the beta bot.\n", encoding="utf-8"
            )
            (fixture_config / "relay.env").write_text(
                f'TELEAGENT_CODEX_HOME="{source_home}"\n', encoding="utf-8"
            )
            (fixture_config / "relay-beta.env").write_text(
                "\n".join(
                    (
                        'TELEAGENT_CODEX_ACCESS_MODE="chat-only"',
                        f'TELEAGENT_INSTANCE_SCRATCH="{scratch}"',
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "TELEAGENT_INSTANCE": "beta",
                    "TELEAGENT_REPO": str(fixture_repo),
                }
            )

            subprocess.run(
                [str(PREPARE_HOME)],
                env=env,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )

            target_home = scratch / "chat-only-codex-home"
            workspace = scratch / "chat-only-workspace"
            config = tomllib.loads(
                (target_home / "config.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                config["permissions"]["tele-agent-chat-only"]["filesystem"],
                {":root": "deny"},
            )
            self.assertIn("You are the beta bot", config["developer_instructions"])
            self.assertEqual(
                config["projects"][str(workspace.resolve())]["trust_level"],
                "trusted",
            )
            self.assertTrue((target_home / "auth.json").is_symlink())
            self.assertEqual(
                (target_home / "auth.json").resolve(),
                (source_home / "auth.json").resolve(),
            )
            self.assertFalse((target_home / "plugins").exists())
            self.assertEqual(list(workspace.iterdir()), [])

            system_skills = target_home / "skills" / ".system"
            bundled_skill = system_skills / "review-agent"
            bundled_skill.mkdir(parents=True)
            (system_skills / ".codex-system-skills.marker").write_text(
                "installed\n", encoding="utf-8"
            )
            (bundled_skill / "SKILL.md").write_text(
                "# Runtime-managed built-in\n", encoding="utf-8"
            )

            rerun = subprocess.run(
                [str(PREPARE_HOME)],
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )

            self.assertEqual(rerun.returncode, 0, rerun.stdout)
            self.assertTrue((bundled_skill / "SKILL.md").is_file())

            custom_skill = target_home / "skills" / "custom" / "SKILL.md"
            custom_skill.parent.mkdir()
            custom_skill.write_text("# Unexpected custom skill\n", encoding="utf-8")
            rejected = subprocess.run(
                [str(PREPARE_HOME)],
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )

            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("contains capability data", rejected.stdout)

    def test_supervisor_omits_legacy_full_access_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture_repo = root / "repo"
            fixture_config = fixture_repo / "config"
            fixture_config.mkdir(parents=True)
            source_home = root / "main-codex-home"
            scratch = root / "beta-scratch"
            args_path = root / "codex-args.txt"
            env_path = root / "codex-env.txt"
            fake_codex = root / "fake-codex"
            source_home.mkdir()
            (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
            (fixture_config / "personality-beta.md").write_text(
                "You are the beta bot.\n", encoding="utf-8"
            )
            (fixture_config / "relay.env").write_text(
                f'TELEAGENT_CODEX_HOME="{source_home}"\n', encoding="utf-8"
            )
            (fixture_config / "relay-beta.env").write_text(
                "\n".join(
                    (
                        'TELEAGENT_CODEX_ACCESS_MODE="chat-only"',
                        f'TELEAGENT_INSTANCE_SCRATCH="{scratch}"',
                        f'TELEAGENT_CODEX_BIN="{fake_codex}"',
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            fake_codex.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    from pathlib import Path
                    import signal
                    import sys

                    Path(os.environ["FAKE_CODEX_ARGS_PATH"]).write_text(
                        "\\n".join(sys.argv[1:]), encoding="utf-8"
                    )
                    Path(os.environ["FAKE_CODEX_ENV_PATH"]).write_text(
                        f"CODEX_HOME={os.environ['CODEX_HOME']}\\n",
                        encoding="utf-8",
                    )
                    os.kill(os.getppid(), signal.SIGTERM)
                    """
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "TELEAGENT_INSTANCE": "beta",
                    "TELEAGENT_REPO": str(fixture_repo),
                    "FAKE_CODEX_ARGS_PATH": str(args_path),
                    "FAKE_CODEX_ENV_PATH": str(env_path),
                }
            )

            process = subprocess.Popen(
                [str(SUPERVISOR)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not args_path.exists():
                    time.sleep(0.05)
                fake_codex_started = args_path.exists()
            finally:
                if process.poll() is None:
                    process.terminate()
                output, _ = process.communicate(timeout=10)
            self.assertTrue(fake_codex_started, output or "fake Codex did not start")
            self.assertEqual(process.returncode, 0, output)

            args = args_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("--strict-config", args)
            self.assertIn("notice.hide_rate_limit_model_nudge=true", args)
            self.assertNotIn("--sandbox", args)
            self.assertNotIn("danger-full-access", args)
            self.assertNotIn("--ask-for-approval", args)
            self.assertEqual(
                args[args.index("--cd") + 1], str(scratch / "chat-only-workspace")
            )
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                f"CODEX_HOME={scratch / 'chat-only-codex-home'}\n",
            )

    def test_registry_accepts_chat_only_session_cwd(self) -> None:
        scripts_dir = REPO_ROOT / "scripts"
        notify_stub = types.ModuleType("notify")
        notify_stub.assert_safe_local_path = lambda path: path
        notify_stub.run_short = lambda *_args, **_kwargs: ""
        previous_notify = sys.modules.get("notify")
        sys.modules["notify"] = notify_stub
        spec = importlib.util.spec_from_file_location(
            "telegram_agent_registry_under_test",
            scripts_dir / "telegram_agent_registry.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        original_sys_path = list(sys.path)
        sys.path.insert(0, str(scripts_dir))
        try:
            spec.loader.exec_module(module)
        finally:
            if previous_notify is None:
                sys.modules.pop("notify", None)
            else:
                sys.modules["notify"] = previous_notify
            sys.path[:] = original_sys_path

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repo = root / "repo"
            workspace = root / "chat-only-workspace"
            repo.mkdir()
            workspace.mkdir()
            session_path = root / "sessions" / "rollout.jsonl"
            session_path.parent.mkdir()
            session_path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "payload": {"cwd": str(workspace)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            meta = {
                "repo_root": str(repo),
                "created_ts": time.time(),
                "launch_source": "start_codex_agent.sh",
            }
            previous_mode = os.environ.get("TELEAGENT_CODEX_ACCESS_MODE")
            previous_workspace = os.environ.get("TELEAGENT_CHAT_ONLY_WORKDIR")
            os.environ["TELEAGENT_CODEX_ACCESS_MODE"] = "chat-only"
            os.environ["TELEAGENT_CHAT_ONLY_WORKDIR"] = str(workspace)
            try:
                self.assertTrue(
                    module.codex_session_matches_agent(meta, session_path)
                )
            finally:
                if previous_mode is None:
                    os.environ.pop("TELEAGENT_CODEX_ACCESS_MODE", None)
                else:
                    os.environ["TELEAGENT_CODEX_ACCESS_MODE"] = previous_mode
                if previous_workspace is None:
                    os.environ.pop("TELEAGENT_CHAT_ONLY_WORKDIR", None)
                else:
                    os.environ["TELEAGENT_CHAT_ONLY_WORKDIR"] = previous_workspace


if __name__ == "__main__":
    unittest.main()
