#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import telegram_inbox  # noqa: E402
from telegram_format import render_telegram_html  # noqa: E402


class TelegramFormattingTests(unittest.TestCase):
    def test_renders_supported_commonmark_and_escapes_code(self) -> None:
        rendered = render_telegram_html(
            "# Result\n\n**bold** and `x < y`\n\n```python\nprint(\"a&b\")\n```"
        )
        self.assertIn("<b>Result</b>", rendered)
        self.assertIn("<b>bold</b>", rendered)
        self.assertIn("<code>x &lt; y</code>", rendered)
        self.assertIn('<pre><code class="language-python">', rendered)
        self.assertIn("a&amp;b", rendered)

    def test_escapes_raw_html_and_blocks_unsafe_links(self) -> None:
        rendered = render_telegram_html(
            '<b>raw</b> [safe](https://example.com/?a=1&b=2) '
            '[unsafe](javascript:alert(1))'
        )
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", rendered)
        self.assertIn('<a href="https://example.com/?a=1&amp;b=2">safe</a>', rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertIn("unsafe", rendered)

    def test_renders_lists_quotes_and_unclosed_markup_safely(self) -> None:
        rendered = render_telegram_html("- one\n- two **unfinished\n\n> quoted")
        self.assertIn("• one", rendered)
        self.assertIn("• two **unfinished", rendered)
        self.assertIn("<blockquote>quoted</blockquote>", rendered)

    def test_code_does_not_overlap_emphasis_links_or_heading_style(self) -> None:
        rendered = render_telegram_html(
            "# Result `x`\n\n**bold `code` tail**\n\n[link `code`](https://example.com)"
        )
        self.assertNotIn("<b><code>", rendered)
        self.assertNotIn("<code>code</code></b>", rendered)
        self.assertIn("<b>bold </b><code>code</code><b> tail</b>", rendered)
        self.assertIn(
            '<a href="https://example.com">link </a><code>code</code>'
            '<a href="https://example.com"></a>',
            rendered,
        )

    def test_code_block_inside_quote_is_flattened(self) -> None:
        rendered = render_telegram_html("> ```python\n> print(1)\n> ```")
        self.assertNotIn("<blockquote><pre", rendered)
        self.assertIn('<pre><code class="language-python">print(1)', rendered)

    def test_send_reply_uses_html_parse_mode(self) -> None:
        with mock.patch.object(telegram_inbox, "telegram_api") as api:
            telegram_inbox.send_reply("token", "123", "**bold**")
        params = api.call_args.args[2]
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertEqual(params["text"], "<b>bold</b>")

    def test_send_reply_attaches_quick_action_keyboard(self) -> None:
        with mock.patch.object(telegram_inbox, "telegram_api") as api:
            telegram_inbox.send_reply("token", "123", "hello")
        params = api.call_args.args[2]
        keyboard = json.loads(params["reply_markup"])
        self.assertEqual(
            keyboard["keyboard"],
            [
                ["/status", "Any news?"],
                ["Fix it.", "Faster."],
                ["It is stagnant? Unhealthy?"],
            ],
        )
        self.assertTrue(keyboard["resize_keyboard"])

    def test_send_reply_falls_back_to_plain_text_on_format_error(self) -> None:
        with mock.patch.object(
            telegram_inbox,
            "telegram_api",
            side_effect=[RuntimeError("Bad Request: can't parse entities"), None],
        ) as api:
            telegram_inbox.send_reply("token", "123", "**bold**")
        self.assertEqual(api.call_count, 2)
        fallback = api.call_args.args[2]
        self.assertEqual(fallback["text"], "**bold**")
        self.assertNotIn("parse_mode", fallback)

    def test_send_reply_does_not_retry_transient_failures(self) -> None:
        with mock.patch.object(
            telegram_inbox,
            "telegram_api",
            side_effect=telegram_inbox.TransientTelegramError("temporary"),
        ) as api:
            with self.assertRaises(telegram_inbox.TransientTelegramError):
                telegram_inbox.send_reply("token", "123", "**bold**")
        api.assert_called_once()

    def test_final_marker_does_not_break_trailing_code_fence(self) -> None:
        outgoing, truncated = telegram_inbox.format_forwarded_agent_message(
            "```python\nprint('ok')\n```", "final_answer", {}, 3600
        )
        self.assertFalse(truncated)
        self.assertTrue(outgoing.endswith("```\n\n∎"))
        rendered = render_telegram_html(outgoing)
        self.assertIn('<pre><code class="language-python">', rendered)
        self.assertNotIn("```", rendered)
        self.assertTrue(rendered.endswith("∎"))

    def test_final_marker_stays_inline_for_normal_prose(self) -> None:
        outgoing, truncated = telegram_inbox.format_forwarded_agent_message(
            "Normal **prose** ending.", "final_answer", {}, 3600
        )
        self.assertFalse(truncated)
        self.assertEqual(outgoing, "Normal **prose** ending. ∎")

    def test_final_marker_is_outside_other_trailing_blocks(self) -> None:
        for source in ("# Heading", "> quote", "- item"):
            with self.subTest(source=source):
                outgoing, _ = telegram_inbox.format_forwarded_agent_message(
                    source, "final_answer", {}, 3600
                )
                self.assertTrue(outgoing.endswith("\n\n∎"))


if __name__ == "__main__":
    unittest.main()
