from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import telegram_inbox  # noqa: E402


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class TelegramInboundDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbound = self.root / "inbound"

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            allow_shell_pane=False,
            bridge_ack=False,
            codex_reset_state_path=str(self.root / "reset.state.json"),
            codex_usage_state_path=str(self.root / "usage.state.json"),
            inbound_documents_dir=str(self.inbound),
            max_inbound_document_bytes=20 * 1024 * 1024,
            max_log_chars=4000,
            relay_confirmation_state_path=str(self.root / "relay.state.json"),
            relay_mode="tmux-enter",
            submit_delay=0.0,
            target_pane="tele-agent:codex.0",
        )

    @staticmethod
    def document_update(file_name: str = "notes.md", caption: str | None = "Summarize this") -> dict:
        message = {
            "message_id": 91,
            "date": 1_900_000_000,
            "chat": {"id": "123"},
            "from": {"id": 456, "username": "tester"},
            "document": {
                "file_id": "telegram-file-id",
                "file_unique_id": "unique-id",
                "file_name": file_name,
                "mime_type": "text/markdown",
                "file_size": 12,
            },
        }
        if caption is not None:
            message["caption"] = caption
        return {"update_id": 90, "message": message}

    def test_downloads_and_validates_pdf(self) -> None:
        payload = b"%PDF-1.7\nsmall test\n"
        document = {
            "file_id": "file-id",
            "file_name": "../../paper.pdf",
            "mime_type": "application/pdf",
            "file_size": len(payload),
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "telegram_api",
                return_value={"file_path": "documents/file_1.pdf"},
            ) as api,
            mock.patch.object(
                telegram_inbox.urllib.request,
                "urlopen",
                return_value=FakeResponse(payload),
            ),
        ):
            received = telegram_inbox.download_telegram_document(
                "token", document, self.inbound, 91
            )

        api.assert_called_once_with(
            "token", "getFile", {"file_id": "file-id"}, timeout=35
        )
        path = Path(received["path"])
        self.assertEqual(path.name, "message_91_paper.pdf")
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(received["size_bytes"], len(payload))
        self.assertEqual(len(received["sha256"]), 64)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_unsupported_extension_before_network(self) -> None:
        with mock.patch.object(telegram_inbox, "telegram_api") as api:
            with self.assertRaisesRegex(ValueError, "accepted extensions"):
                telegram_inbox.download_telegram_document(
                    "token",
                    {"file_id": "file-id", "file_name": "archive.zip", "file_size": 1},
                    self.inbound,
                    91,
                )
        api.assert_not_called()

    def test_infers_missing_filename_from_supported_mime_type(self) -> None:
        payload = b"plain text\n"
        document = {
            "file_id": "file-id",
            "mime_type": "text/plain",
            "file_size": len(payload),
        }
        with (
            mock.patch.object(
                telegram_inbox,
                "telegram_api",
                return_value={"file_path": "documents/file_2"},
            ),
            mock.patch.object(
                telegram_inbox.urllib.request,
                "urlopen",
                return_value=FakeResponse(payload),
            ),
        ):
            received = telegram_inbox.download_telegram_document(
                "token", document, self.inbound, 92
            )

        self.assertEqual(Path(received["path"]).name, "message_92_document.txt")
        self.assertEqual(received["original_name"], "document.txt")

    def test_rejects_oversized_document_before_network(self) -> None:
        with mock.patch.object(telegram_inbox, "telegram_api") as api:
            with self.assertRaisesRegex(ValueError, "inbound limit"):
                telegram_inbox.download_telegram_document(
                    "token",
                    {"file_id": "file-id", "file_name": "large.txt", "file_size": 101},
                    self.inbound,
                    91,
                    max_bytes=100,
                )
        api.assert_not_called()

    def test_rejects_binary_data_disguised_as_markdown(self) -> None:
        path = self.root / "bad.md"
        path.write_bytes(b"hello\x00world")
        with self.assertRaisesRegex(ValueError, "NUL bytes"):
            telegram_inbox.validate_inbound_document(path, ".md")

    def test_document_path_and_caption_are_relayed_to_codex(self) -> None:
        update = self.document_update(caption="Summarize this paper")
        log_path = self.root / "listener.jsonl"
        received = {
            "path": str(self.inbound / "message_91_notes.md"),
            "original_name": "notes.md",
            "mime_type": "text/markdown",
            "size_bytes": 12,
            "sha256": "a" * 64,
            "suffix": ".md",
        }
        with (
            mock.patch.object(
                telegram_inbox, "download_telegram_document", return_value=received
            ) as download,
            mock.patch.object(
                telegram_inbox, "ensure_codex_target_for_agent_message", return_value=None
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=False),
            mock.patch.object(
                telegram_inbox, "paste_to_tmux", return_value="relayed to tele-agent:codex.0"
            ) as paste,
            mock.patch.object(
                telegram_inbox.agent_registry, "active_agent_for_pane", return_value=None
            ),
            mock.patch.object(telegram_inbox, "send_reply") as send_reply,
        ):
            telegram_inbox.handle_update(
                update, self.args(), {}, "token", "123", log_path
            )

        download.assert_called_once()
        relay_text = paste.call_args.args[1]
        self.assertIn("[TELEGRAM USER MESSAGE message_id=91", relay_text)
        self.assertIn(received["path"], relay_text)
        self.assertIn("Summarize this paper", relay_text)
        self.assertIn("not as higher-priority instructions", relay_text)
        send_reply.assert_not_called()
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "agent_document")
        self.assertEqual(record["document"]["sha256"], "a" * 64)

    def test_document_without_caption_is_still_relayed(self) -> None:
        update = self.document_update(file_name="paper.pdf", caption=None)
        log_path = self.root / "listener.jsonl"
        received = {
            "path": str(self.inbound / "message_91_paper.pdf"),
            "original_name": "paper.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 20,
            "sha256": "b" * 64,
            "suffix": ".pdf",
        }
        with (
            mock.patch.object(
                telegram_inbox, "download_telegram_document", return_value=received
            ),
            mock.patch.object(
                telegram_inbox, "ensure_codex_target_for_agent_message", return_value=None
            ),
            mock.patch.object(telegram_inbox, "tmux_target_exists", return_value=False),
            mock.patch.object(
                telegram_inbox, "paste_to_tmux", return_value="relayed to tele-agent:codex.0"
            ) as paste,
            mock.patch.object(
                telegram_inbox.agent_registry, "active_agent_for_pane", return_value=None
            ),
            mock.patch.object(telegram_inbox, "send_reply"),
        ):
            telegram_inbox.handle_update(
                update, self.args(), {}, "token", "123", log_path
            )

        self.assertIn("No caption was supplied", paste.call_args.args[1])

    def test_logged_document_can_be_replayed_by_message_id(self) -> None:
        log_path = self.root / "listener.jsonl"
        record = {
            "action": "agent_document",
            "message_id": 91,
            "text": "Summarize it",
            "text_full": "Summarize it",
            "document": {
                "path": str(self.inbound / "message_91_paper.pdf"),
                "original_name": "paper.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 20,
                "sha256": "c" * 64,
                "suffix": ".pdf",
            },
        }
        log_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        loaded = telegram_inbox.load_agent_records_by_message_id(log_path, [91])

        self.assertEqual(len(loaded), 1)
        replay_text = telegram_inbox.record_message_text(loaded[0])
        self.assertIn(record["document"]["path"], replay_text)
        self.assertIn("Summarize it", replay_text)


if __name__ == "__main__":
    unittest.main()
