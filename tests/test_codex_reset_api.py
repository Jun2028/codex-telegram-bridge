from __future__ import annotations

import argparse
from contextlib import ExitStack
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
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.send = self.contexts.enter_context(mock.patch.object(transport, "send_reply"))
        self.contexts.enter_context(mock.patch.object(processes, "codex_executable", return_value="codex"))
        self.contexts.enter_context(mock.patch.object(commands.agent_registry, "active_agent_for_pane", return_value=None))
        self.start = self.contexts.enter_context(mock.patch.object(
            lifecycle, "start_codex_agent",
            return_value=("fixture:codex.0", "started", {"agent_id": "fixture"}),
        ))
        self.contexts.enter_context(mock.patch.dict(os.environ, {
            "TELEAGENT_CODEX_HOME": str(self.root / "account"),
            "CODEX_HOME": str(self.root / "wrong-account"),
        }))
        self.legacy = self.contexts.enter_context(mock.patch.object(
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
import json, os, pathlib, sys, time
assert sys.argv[1] == "app-server"
home = pathlib.Path(os.environ["CODEX_HOME"])
account = (home / "auth.json").resolve().parent if (home / "auth.json").exists() else home
with (account / "launches.jsonl").open("a") as log:
    log.write(json.dumps({"pid": os.getpid(), "home": str(home)}) + "\\n")
if (account / "stall-first-startup").exists():
    (account / "stall-first-startup").unlink()
    time.sleep(10)
if (account / "blocked-backfill").exists():
    if home == account:
        time.sleep(10)
    overrides = dict(arg.split("=", 1) for arg in sys.argv[3::2])
    assert json.loads(overrides["sqlite_home"]) == str(home / "sqlite")
    assert json.loads(overrides["log_dir"]) == str(home / "log")
    assert pathlib.Path.cwd() == home.resolve()
    assert not (home / "sessions").exists()
    assert not (home / "state_5.sqlite").exists()
    assert (home / "config.toml").resolve() == account / "config.toml"
    assert (home / "auth.json").read_text() == "fixture-account"
    (home / "auth.json").write_text("refreshed-fixture-account")
    (account / "helper-path").write_text(str(home))
for line in sys.stdin:
    request = json.loads(line)
    with (account / "requests.jsonl").open("a") as log:
        log.write(line)
    if request["method"] == "initialize":
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif request["method"] != "initialized":
        if (account / "stall-account-request").exists():
            time.sleep(10)
        response = json.loads((account / "response.json").read_text())
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

    def test_file_account_query_bypasses_blocked_agent_database_and_preserves_refresh(self):
        (self.root / "auth.json").write_text("fixture-account")
        (self.root / "config.toml").write_text('sqlite_home = "/agent/database"\n')
        (self.root / "blocked-backfill").touch()
        (self.root / "state_5.sqlite").write_bytes(b"live database: do not modify")
        self.respond({"result": snapshot()["result"]})
        with mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": "/agent/inherited-database"}):
            live = codex_rate_limits.read_rate_limits(
                str(self.cli), codex_home=self.root, workdir=self.root, timeout=2,
            )
        self.assertEqual(codex_rate_limits.remaining_percentages(live), [0])
        self.assertEqual(live["result"]["rateLimitResetCredits"]["availableCount"], 3)
        self.assertEqual((self.root / "auth.json").read_text(), "refreshed-fixture-account")
        self.assertEqual((self.root / "state_5.sqlite").read_bytes(), b"live database: do not modify")
        self.assertFalse(Path((self.root / "helper-path").read_text()).exists())
        self.assertEqual([item["method"] for item in self.requests()], [
            "initialize", "initialized", "account/rateLimits/read",
        ])

    def test_non_file_credentials_keep_original_account_home(self):
        (self.root / "auth.json").write_text("possibly stale file credentials")
        for setting in ('"keyring"', '"auto"', '"ephemeral"', '"""file"""'):
            with self.subTest(setting=setting):
                (self.root / "config.toml").write_text(
                    '[profiles.alternate]\n"cli_auth_credentials_store" = ' + setting + '\n'
                )
                with codex_rate_limits._account_runtime(self.root, self.root) as runtime:
                    self.assertEqual(runtime, (self.root, self.root, []))

    def test_helper_cleanup_on_failure_does_not_delete_shared_credentials(self):
        (self.root / "auth.json").write_text("fixture-account")
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with codex_rate_limits._account_runtime(self.root, self.root) as (home, _, _):
                self.assertEqual(home.stat().st_mode & 0o777, 0o700)
                self.assertTrue((home / "auth.json").samefile(self.root / "auth.json"))
                raise RuntimeError("fixture failure")
        self.assertFalse(home.exists())
        self.assertEqual((self.root / "auth.json").read_text(), "fixture-account")

    def test_startup_timeout_is_not_reported_as_an_account_request_timeout(self):
        (self.root / "blocked-backfill").touch()
        with self.assertRaisesRegex(codex_rate_limits.RateLimitError, "app-server startup timed out"):
            codex_rate_limits.read_rate_limits(
                str(self.cli), codex_home=self.root, workdir=self.root, timeout=0.1,
            )

    def test_stalled_startup_recovers_with_fresh_helper_before_account_read(self):
        (self.root / "auth.json").write_text("fixture-account")
        (self.root / "stall-first-startup").touch()
        self.respond({"result": snapshot()["result"]})
        live = codex_rate_limits.read_rate_limits(
            str(self.cli), codex_home=self.root, workdir=self.root, timeout=2,
        )
        self.assertEqual(codex_rate_limits.remaining_percentages(live), [0])
        self.assertEqual([item["method"] for item in self.requests()], [
            "initialize", "initialized", "account/rateLimits/read",
        ])
        launches = [json.loads(line) for line in (self.root / "launches.jsonl").read_text().splitlines()]
        self.assertEqual(len(launches), 2)
        self.assertNotEqual(launches[0]["home"], launches[1]["home"])
        for launch in launches:
            self.assertFalse(Path(launch["home"]).exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(launch["pid"], 0)

    def test_confirmed_reset_recovers_startup_without_sending_reset_twice(self):
        (self.root / "stall-first-startup").touch()
        self.respond({"result": {"outcome": "reset"}})
        result = codex_rate_limits.consume_reset_credit(
            str(self.cli), codex_home=self.root, workdir=self.root,
            idempotency_key="already-confirmed", timeout=2,
        )
        self.assertEqual(result, "reset")
        requests = [item for item in self.requests() if item["method"].endswith("/consume")]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["params"], {"idempotencyKey": "already-confirmed"})

    def test_account_response_timeout_never_retries_a_possible_redemption(self):
        (self.root / "stall-account-request").touch()
        with self.assertRaisesRegex(codex_rate_limits.RateLimitError, "consume request timed out"):
            codex_rate_limits.consume_reset_credit(
                str(self.cli), codex_home=self.root, workdir=self.root,
                idempotency_key="already-confirmed", timeout=0.5,
            )
        self.assertEqual(len((self.root / "launches.jsonl").read_text().splitlines()), 1)
        self.assertEqual([item["method"] for item in self.requests()], [
            "initialize", "initialized", "account/rateLimitResetCredit/consume",
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

    def test_confirmed_reset_uses_isolated_runtime_and_same_account_and_key(self):
        (self.root / "auth.json").write_text("fixture-account")
        (self.root / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        (self.root / "blocked-backfill").touch()
        self.respond({"result": {"outcome": "alreadyRedeemed"}})
        result = codex_rate_limits.consume_reset_credit(
            str(self.cli), codex_home=self.root, workdir=self.root,
            idempotency_key="previously-confirmed-key", timeout=2,
        )
        self.assertEqual(result, "alreadyRedeemed")
        self.assertEqual(self.requests()[-1], {
            "id": 2, "method": "account/rateLimitResetCredit/consume",
            "params": {"idempotencyKey": "previously-confirmed-key"},
        })
        self.assertFalse(Path((self.root / "helper-path").read_text()).exists())

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
