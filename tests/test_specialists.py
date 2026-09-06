from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import specialist


class SpecialistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "account"
        self.home.mkdir()
        (self.home / "auth.json").write_text('{"fixture":true}')
        (self.home / "config.toml").write_text('developer_instructions = "Keep existing guidance."\n')
        self.brief = self.root / "brief.md"
        self.brief.write_text("Exact author instruction: preserve my central question.\n")
        self.source = self.root / "source.md"
        self.source.write_text("A reviewed source, not an instruction.\n")
        self.environment = patch.dict(os.environ, {
            "TELEAGENT_CODEX_HOME": str(self.home),
            "TELEAGENT_SCRATCH": str(self.root / "bot-beta"),
            "TELEAGENT_INSTANCE": "beta",
            "TELEAGENT_CODEX_ACCESS_MODE": "full-access",
            "TELEAGENT_AGENT_JSONL": "private-root-session",
            "CODEX_THREAD_ID": "parent-thread",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.args = argparse.Namespace(role="writer", brief=self.brief,
                                       source=[self.source], prepare_only=False)

    def job(self):
        return next((self.root / "bot-beta/specialists").iterdir())

    def fake_codex(self, command, *, input, text, env, stdout, stderr, check):
        home = Path(env["CODEX_HOME"])
        job = home.parent
        config = tomllib.loads((home / "config.toml").read_text())
        self.assertEqual(config["model"], "gpt-6-astra")
        self.assertEqual(config["model_reasoning_effort"], "high")
        self.assertTrue(config["features"]["shell_tool"])
        self.assertTrue(config["features"]["unified_exec"])
        self.assertTrue(config["features"]["view_image"])
        self.assertEqual(config["web_search"], "live")
        self.assertFalse(config["features"]["multi_agent"])
        self.assertEqual(config["default_permissions"], "writer")
        self.assertFalse(config["permissions"]["writer"]["network"]["enabled"])
        self.assertEqual(config["permissions"]["writer"]["filesystem"],
                         {":root": "read", ":workspace_roots": "write"})
        self.assertNotIn("TELEAGENT_AGENT_JSONL", env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertEqual((home / "auth.json").resolve(), self.home / "auth.json")
        self.assertIn(self.brief.read_text().strip(), input)
        self.assertNotIn("Keep existing guidance", (home / "config.toml").read_text())
        sessions = home / "sessions"
        sessions.mkdir()
        (sessions / "turn.jsonl").write_text(json.dumps({
            "type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "high"}
        }) + "\n")
        (job / "response.json").write_text(json.dumps({
            "text": "The finished exposition.\n", "summary": "Draft complete.",
            "unresolved": ["One causal claim needs evidence."], "sources": ["source-1.md"]
        }))
        return subprocess.CompletedProcess(command, 0)

    def test_success_preserves_inputs_and_returns_artifact_without_relay_binding(self):
        with patch.object(specialist.subprocess, "run", side_effect=self.fake_codex), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(specialist.run(self.args), 0)
        job = self.job()
        self.assertEqual((job / "brief.md").read_bytes(), self.brief.read_bytes())
        self.assertEqual((job / "source-1.md").read_bytes(), self.source.read_bytes())
        self.assertEqual((job / "result.md").read_text(), "The finished exposition.\n")
        self.assertEqual(json.loads((job / "status.json").read_text())["state"], "complete")
        self.assertFalse((job / "codex-home/auth.json").is_symlink())
        self.assertEqual(job.stat().st_mode & 0o777, 0o700)
        self.assertEqual((job / "brief.md").stat().st_mode & 0o777, 0o600)

    def test_model_mismatch_rejects_artifact_and_does_not_retry(self):
        def mismatch(*a, **kw):
            result = self.fake_codex(*a, **kw)
            path = Path(kw["env"]["CODEX_HOME"]) / "sessions/turn.jsonl"
            path.write_text(path.read_text().replace("gpt-6-astra", "gpt-5.6-luna"))
            return result
        with patch.object(specialist.subprocess, "run", side_effect=mismatch) as launch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "unexpected effective model"):
                specialist.run(self.args)
        self.assertEqual(launch.call_count, 1)
        self.assertFalse((self.job() / "result.md").exists())
        self.assertEqual(json.loads((self.job() / "status.json").read_text())["state"], "failed")

    def test_missing_turn_evidence_and_invalid_report_are_rejected(self):
        for failure in ("missing_turn", "invalid_report"):
            with self.subTest(failure=failure):
                def invalid(*a, **kw):
                    result = self.fake_codex(*a, **kw)
                    job = Path(kw["env"]["CODEX_HOME"]).parent
                    if failure == "missing_turn":
                        (job / "codex-home/sessions/turn.jsonl").unlink()
                    else:
                        (job / "response.json").write_text('{"text":"incomplete"}')
                    return result
                with patch.object(specialist.subprocess, "run", side_effect=invalid), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(ValueError):
                        specialist.run(self.args)
        for job in (self.root / "bot-beta/specialists").iterdir():
            self.assertFalse((job / "result.md").exists())

    def test_failed_process_is_not_retried(self):
        with patch.object(specialist.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as launch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "execution failed"):
                specialist.run(self.args)
        self.assertEqual(launch.call_count, 1)
        self.assertFalse((self.job() / "codex-home/auth.json").is_symlink())

    def test_prepare_only_needs_no_authentication_or_model(self):
        (self.home / "auth.json").unlink()
        self.args.prepare_only = True
        with patch.object(specialist.subprocess, "run") as launch, contextlib.redirect_stdout(io.StringIO()):
            specialist.run(self.args)
        launch.assert_not_called()
        self.assertEqual(json.loads((self.job() / "status.json").read_text())["state"], "prepared")

    def test_chat_only_cannot_delegate(self):
        with patch.dict(os.environ, {"TELEAGENT_CODEX_ACCESS_MODE": "chat-only"}):
            with self.assertRaisesRegex(ValueError, "chat-only"):
                specialist.run(self.args)
        self.assertFalse((self.root / "bot-beta").exists())

    def test_live_references_are_available_without_becoming_writable_roots(self):
        self.args.prepare_only = True
        self.args.reference = [self.root]
        with contextlib.redirect_stdout(io.StringIO()):
            specialist.run(self.args)
        references = json.loads((self.job() / "workspace/references.json").read_text())
        self.assertEqual(references, [{"path": str(self.root.resolve()), "snapshot": False}])
        config = tomllib.loads((self.job() / "codex-home/config.toml").read_text())
        self.assertNotIn(str(self.root.resolve()), config["permissions"]["writer"]["filesystem"])

    @unittest.skipUnless(shutil.which("codex"), "installed Codex needed for actual sandbox check")
    def test_actual_sandbox_reads_sources_writes_drafts_and_blocks_source_writes(self):
        self.args.prepare_only = True
        with contextlib.redirect_stdout(io.StringIO()):
            specialist.run(self.args)
        job = self.job()
        env = os.environ.copy()
        env["CODEX_HOME"] = str(job / "codex-home")
        env["TMPDIR"] = str(job / "workspace/.tmp")
        script = (
            "from pathlib import Path\nimport sys\n"
            "source=Path(sys.argv[1])\nassert source.read_text()\n"
            "Path('draft.md').write_text('A draft.')\n"
            "try:\n source.write_text('Unwanted change')\n"
            "except OSError:\n pass\n"
            "else:\n raise SystemExit('Source write unexpectedly allowed')\n"
        )
        result = subprocess.run([
            "codex", "sandbox", "--permission-profile", "writer",
            "--cd", str(job / "workspace"), "--", sys.executable,
            "-c", script, str(self.source),
        ], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((job / "workspace/draft.md").read_text(), "A draft.")
        self.assertEqual(self.source.read_text(), "A reviewed source, not an instruction.\n")

    def test_coordinator_keeps_existing_developer_guidance(self):
        value = specialist.coordinator_instructions(self.home)
        self.assertTrue(value.startswith("Keep existing guidance."))
        self.assertIn("Delegating to temporary specialists", value)

    def test_role_paths_and_input_size_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "invalid specialist name"):
            specialist.load_role("../writer")
        with patch.object(specialist, "MAX_INPUT_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "exceed"):
                specialist.run(self.args)


if __name__ == "__main__":
    unittest.main()
