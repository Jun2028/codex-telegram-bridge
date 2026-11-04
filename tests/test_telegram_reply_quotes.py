from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from teleagent import messages  # noqa: E402


class TelegramReplyQuoteTests(unittest.TestCase):
    def test_selected_sentence_is_distinguished_from_the_full_reply(self) -> None:
        selected = "The work that is easiest to automate should not set our ambitions."
        original = f"The opening paragraph.\n{selected}\nThe final paragraph."
        for text_key in ("text", "caption"):
            with self.subTest(text_key=text_key):
                message = {
                    "message_id": 22,
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 456},
                    "text": "this sentence feels disjoint",
                    "reply_to_message": {
                        "message_id": 21,
                        "from": {"id": 789, "first_name": "Relay Bot"},
                        text_key: original,
                    },
                    "quote": {"text": selected, "position": 23, "is_manual": True},
                }

                formatted = messages.format_agent_message(message, message["text"])

                self.assertIn(
                    f"\n{selected}\n[/QUOTED PART OF TELEGRAM MESSAGE]",
                    formatted,
                )
                self.assertNotIn(original, formatted)

    def test_quote_text_is_preserved_exactly_instead_of_sliced_by_position(self) -> None:
        selected = "  数学 🧮\nβ  "
        message = {
            "reply_to_message": {"text": "🧠 repeated: 数学 🧮\nβ, then again: 数学 🧮\nβ"},
            "quote": {"text": selected, "position": 28, "is_manual": True},
        }

        formatted = messages.format_reply_context(message)

        self.assertIn(f"\n{selected}\n[/QUOTED PART OF TELEGRAM MESSAGE]", formatted)

    def test_quote_survives_without_an_accessible_replied_message(self) -> None:
        message = {
            "external_reply": {"origin": {"type": "hidden_user", "sender_user_name": "Author"}},
            "quote": {"text": "Selected external passage", "position": 0, "is_manual": True},
        }

        formatted = messages.format_reply_context(message)
        self.assertIn("[QUOTED PART OF TELEGRAM MESSAGE", formatted)
        self.assertIn("\nSelected external passage\n[/QUOTED PART OF TELEGRAM MESSAGE]", formatted)


    def test_missing_or_invalid_quote_keeps_existing_reply_context(self) -> None:
        message = {"reply_to_message": {"message_id": 21, "text": "Full original text"}}
        expected = messages.format_reply_context(message)
        for quote in (None, "text", [], {}, {"text": None}, {"text": 123}, {"text": " \n"}):
            with self.subTest(quote=quote):
                self.assertEqual(
                    messages.format_reply_context({**message, "quote": quote}), expected
                )
        self.assertEqual(messages.format_reply_context({}), "")

    def test_invalid_position_does_not_hide_valid_quote_text(self) -> None:
        for position in (None, -1, True, "3"):
            with self.subTest(position=position):
                formatted = messages.format_reply_context(
                    {"quote": {"text": "Exact selection", "position": position, "is_manual": True}}
                )
                self.assertIn("\nExact selection\n[/QUOTED PART OF TELEGRAM MESSAGE]", formatted)
                self.assertNotIn("position_utf16=", formatted)


if __name__ == "__main__":
    unittest.main()
