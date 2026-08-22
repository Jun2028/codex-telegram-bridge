"""Tests for Telegram voice note handling."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox as inbox  # noqa: E402


class TelegramInboundVoiceTests(unittest.TestCase):
    def _tmp_bin(self, content: str = "#!/bin/sh\nexit 0\n") -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "tool"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_voice_tool_problem_reports_missing_binary(self) -> None:
        missing = Path("/nonexistent/whisper-cli")
        model = Path("/nonexistent/model.bin")
        opus = self._tmp_bin()
        problem = inbox.voice_tool_problem(missing, model, opus)
        self.assertIsNotNone(problem)
        self.assertIn("whisper binary", problem)

    def test_voice_tool_problem_ok_when_all_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake")
            problem = inbox.voice_tool_problem(self._tmp_bin(), model, self._tmp_bin())
        self.assertIsNone(problem)

    def test_format_voice_text_includes_transcript_and_caption(self) -> None:
        text = inbox.format_voice_text("hello agent", "some caption")
        self.assertIn("hello agent", text)
        self.assertIn("some caption", text)
        self.assertIn("voice note", text.lower())

    def test_download_voice_requires_file_id(self) -> None:
        with self.assertRaises(ValueError):
            inbox.download_telegram_voice(
                "token", {}, Path("/tmp"), 1, max_bytes=100
            )

    def test_transcribe_fails_fast_when_tools_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ogg = Path(tmp) / "note.ogg"
            ogg.write_bytes(b"fake")
            with self.assertRaises(RuntimeError) as ctx:
                inbox.transcribe_voice_ogg(
                    ogg,
                    Path("/nonexistent/whisper-cli"),
                    Path("/nonexistent/model.bin"),
                    Path("/nonexistent/opusdec"),
                    Path("/nonexistent/lib"),
                )
        self.assertIn("unavailable on this host", str(ctx.exception))

    @unittest.skipUnless(
        Path.home().joinpath("whisper.cpp/build/bin/whisper-cli").is_file()
        and Path.home().joinpath("voice/usr/bin/opusdec").is_file()
        and os.environ.get("TELEAGENT_VOICE_E2E") == "1",
        "live voice E2E requires installed whisper/opusdec and explicit opt-in",
    )
    def test_live_transcription_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "t.wav"
            # Reuse the synthesized sample when available.
            if Path("/tmp/hello.wav").is_file():
                ogg = Path(tmp) / "t.ogg"
                import subprocess
                env = dict(os.environ)
                env["LD_LIBRARY_PATH"] = str(
                    Path.home() / "voice/usr/lib/x86_64-linux-gnu"
                )
                subprocess.run(
                    [str(Path.home() / "voice/usr/bin/opusenc"), "--quiet",
                     "/tmp/hello.wav", str(ogg)],
                    env=env, check=True,
                )
                transcript = inbox.transcribe_voice_ogg(
                    ogg,
                    Path.home() / "whisper.cpp/build/bin/whisper-cli",
                    Path.home() / "whisper.cpp/models/ggml-small.bin",
                    Path.home() / "voice/usr/bin/opusdec",
                    Path.home() / "voice/usr/lib/x86_64-linux-gnu",
                )
                self.assertTrue(transcript.strip())


if __name__ == "__main__":
    unittest.main()
