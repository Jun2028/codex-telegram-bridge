from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_rate_limits
from teleagent import auth, commands, lifecycle, processes, state, transport


def snapshot(used: int = 100, count: int = 3, rows=None) -> dict:
    return {
        "method": "account/rateLimits/read",
        "fetched_at": "2026-09-14T05:00:00+00:00",
        "result": {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": used, "windowDurationMins": 300},
            },
            "rateLimitResetCredits": {"availableCount": count, "credits": rows},
        },
    }


class CodexResetCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.args = argparse.Namespace(
            repo_root=self.root,
            codex_usage_state_path=str(self.root / "usage.json"),
            codex_reset_state_path=str(self.root / "reset.json"),
            target_pane="fixture:codex.0",
            session="fixture",
            codex_window="codex",
            max_log_chars=4000,
        )
        self.number = 0
        self.send = self.enterContext(mock.patch.object(transport, "send_reply"))
        self.enterContext(mock.patch.object(processes, "codex_executable", return_value="codex"))
        self.enterContext(mock.patch.object(commands.agent_registry, "active_agent_for_pane", return_value=None))
        self.start = self.enterContext(mock.patch.object(
            lifecycle, "start_codex_agent",
            return_value=("fixture:codex.0", "started", {"agent_id": "fixture"}),
        ))
        self.enterContext(mock.patch.dict(os.environ, {
            "TELEAGENT_CODEX_HOME": str(self.root / "account"),
            "CODEX_HOME": str(self.root / "wrong-account"),
        }))
        self.legacy = self.enterContext(mock.patch.object(
            auth, "run_codex_reset_helper", side_effect=AssertionError("legacy helper called"),
        ))

    def command(self, text):
        self.number += 1
        return commands.handle_update(
            {"update_id": self.number, "message": {
                "message_id": self.number, "date": int(time.time()),
                "chat": {"id": "123"}, "from": {"id": 456}, "text": text,
            }},
            self.args, {}, "token", "123", self.root / "inbox.jsonl",
        )

    def reset_state(self):
        return state.read_json_object(Path(self.args.codex_reset_state_path))

    def test_listener_uses_configured_home_without_shell_codex_home(self):
        for inherited in (None, str(self.root / "wrong-account")):
            with self.subTest(inherited=inherited), mock.patch.dict(os.environ):
                if inherited is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = inherited
                with mock.patch.object(codex_rate_limits, "read_rate_limits", return_value=snapshot()) as read:
                    auth.inspect_codex_live_usage(self.root)
                self.assertEqual(read.call_args.kwargs["codex_home"], self.root / "account")

    def test_exhausted_account_lists_resets_without_spending_or_watchdog(self):
        with (
            mock.patch.object(auth, "inspect_codex_live_usage", return_value=snapshot()) as read,
            mock.patch.object(codex_rate_limits, "consume_reset_credit") as consume,
        ):
            result = self.command("/codex_reset")
        self.assertEqual(result["action"], "codex_reset_confirmation_requested")
        read.assert_called_once()
        consume.assert_not_called()
        self.legacy.assert_not_called()
        self.start.assert_not_called()
        reply = self.send.call_args.args[2]
        self.assertIn("0% left", reply)
        self.assertIn("Banked Codex resets remaining: 3", reply)
        self.assertIn("Send /Confirm", reply)
        self.assertTrue(self.reset_state()["idempotency_key"])

    def test_available_count_is_authoritative_when_details_are_partial(self):
        for rows in (None, [], [{"status": "available", "expiresAt": None}]):
            with self.subTest(rows=rows):
                count, entries = auth.list_codex_usage_resets(self.root, live=snapshot(rows=rows))
                self.assertEqual(count, 3)
                self.assertLess(len(entries), count)

    def test_unreported_credits_are_not_reported_as_zero(self):
        live = snapshot()
        live["result"]["rateLimitResetCredits"] = None
        with mock.patch.object(auth, "inspect_codex_live_usage", return_value=live):
            result = self.command("/codex_reset")
        self.assertEqual(result["action"], "codex_reset_list_failed")
        self.assertNotIn("remaining: 0", self.send.call_args.args[2])
        self.assertNotIn("Send /Confirm", self.send.call_args.args[2])

    def test_failed_account_query_invalidates_an_earlier_confirmation(self):
        with mock.patch.object(auth, "inspect_codex_live_usage", return_value=snapshot()):
            self.command("/codex_reset")
        with mock.patch.object(
            auth, "inspect_codex_live_usage",
            side_effect=codex_rate_limits.RateLimitError("account authentication required"),
        ) as read:
            self.command("/codex_reset")
        read.assert_called_once()
        reply = self.send.call_args.args[2]
        self.assertEqual(reply.count("account authentication required"), 1)
        self.assertIn("No reset was redeemed", reply)
        self.assertEqual(self.reset_state()["phase"], "check_failed")
        with mock.patch.object(auth, "redeem_codex_usage_reset") as redeem:
            self.command("/Confirm")
        redeem.assert_not_called()

    def test_no_credit_or_eligible_window_does_not_restart_or_claim_success(self):
        for outcome in ("noCredit", "nothingToReset"):
            with self.subTest(outcome=outcome), (
                mock.patch.object(auth, "inspect_codex_live_usage", return_value=snapshot())
            ), mock.patch.object(codex_rate_limits, "consume_reset_credit", return_value=outcome):
                self.command("/codex_reset")
                self.command("/Confirm")
                saved = self.reset_state()
                self.assertEqual(saved["phase"], "unavailable")
                self.assertFalse(saved["redeemed"])
                self.assertIn("No reset was redeemed", self.send.call_args.args[2])
                self.assertNotIn("could not be confirmed", self.send.call_args.args[2])
        self.start.assert_not_called()

    def test_already_redeemed_is_success_and_uses_the_saved_key(self):
        with (
            mock.patch.object(auth, "inspect_codex_live_usage", side_effect=[snapshot(), snapshot(used=0)]) as read,
            mock.patch.object(codex_rate_limits, "consume_reset_credit", return_value="alreadyRedeemed") as consume,
        ):
            self.command("/codex_reset")
            key = self.reset_state()["idempotency_key"]
            self.command("/Confirm")
            self.command("/Confirm")
        consume.assert_called_once()
        self.assertEqual(consume.call_args.kwargs["idempotency_key"], key)
        self.assertEqual(consume.call_args.kwargs["codex_home"], self.root / "account")
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.reset_state()["phase"], "completed")
        self.start.assert_called_once()

    def test_refresh_failure_preserves_redemption_without_spending_again(self):
        with (
            mock.patch.object(auth, "inspect_codex_live_usage", side_effect=[snapshot(), RuntimeError("query timed out")]),
            mock.patch.object(codex_rate_limits, "consume_reset_credit", return_value="reset") as consume,
        ):
            self.command("/codex_reset")
            self.command("/Confirm")
            self.assertIn("Do not redeem another reset", self.send.call_args.args[2])
            self.command("/Confirm")
        consume.assert_called_once()
        saved = self.reset_state()
        self.assertTrue(saved["redeemed"])
        self.assertEqual(saved["phase"], "redeemed_followup_failed")
        self.assertEqual(saved["failed_stage"], "refresh usage state")
        self.start.assert_not_called()

    def test_account_change_requires_a_new_confirmation(self):
        with mock.patch.object(auth, "inspect_codex_live_usage", return_value=snapshot()):
            self.command("/codex_reset")
        with (
            mock.patch.dict(os.environ, {"TELEAGENT_CODEX_HOME": str(self.root / "another-account")}),
            mock.patch.object(codex_rate_limits, "consume_reset_credit") as consume,
        ):
            self.command("/Confirm")
        consume.assert_not_called()
        self.start.assert_not_called()
        self.assertIn("account configuration changed", self.send.call_args.args[2])


class CodexAccountProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cli = self.root / "codex"
        self.cli.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
assert sys.argv[1:] == ["app-server"]
home = pathlib.Path(os.environ["CODEX_HOME"])
for line in sys.stdin:
    request = json.loads(line)
    with (home / "requests.jsonl").open("a") as log:
        log.write(line)
    if request["method"] == "initialize":
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif request["method"] != "initialized":
        response = json.loads((home / "response.json").read_text())
        print(json.dumps({"id": request["id"], **response}), flush=True)
''')
        self.cli.chmod(0o700)

    def respond(self, response):
        (self.root / "response.json").write_text(json.dumps(response))

    def requests(self):
        return [json.loads(line) for line in (self.root / "requests.jsonl").read_text().splitlines()]

    def test_exhausted_account_read_uses_only_account_rpc(self):
        self.respond({"result": snapshot()["result"]})
        live = codex_rate_limits.read_rate_limits(str(self.cli), codex_home=self.root, workdir=self.root)
        self.assertEqual(codex_rate_limits.remaining_percentages(live), [0])
        self.assertEqual([item["method"] for item in self.requests()], [
            "initialize", "initialized", "account/rateLimits/read",
        ])

    def test_reset_rpc_preserves_outcomes_and_idempotency_key(self):
        for outcome in ("reset", "alreadyRedeemed", "nothingToReset", "noCredit"):
            with self.subTest(outcome=outcome):
                self.respond({"result": {"outcome": outcome}})
                result = codex_rate_limits.consume_reset_credit(
                    str(self.cli), codex_home=self.root, workdir=self.root,
                    idempotency_key="saved-at-confirmation",
                )
                self.assertEqual(result, outcome)
                self.assertEqual(self.requests()[-1], {
                    "id": 2, "method": "account/rateLimitResetCredit/consume",
                    "params": {"idempotencyKey": "saved-at-confirmation"},
                })

    def test_unknown_or_failed_redemption_is_not_success(self):
        for response in ({"result": {"outcome": "unexpected"}}, {"error": {"message": "unavailable"}}):
            with self.subTest(response=response):
                self.respond(response)
                with self.assertRaises(codex_rate_limits.RateLimitError):
                    codex_rate_limits.consume_reset_credit(
                        str(self.cli), codex_home=self.root, workdir=self.root,
                        idempotency_key="saved-at-confirmation",
                    )


if __name__ == "__main__":
    unittest.main()
