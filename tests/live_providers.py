#!/usr/bin/env python3
"""Run the relay against the real provider CLIs installed on this machine.

    python tests/live_providers.py                 every provider
    python tests/live_providers.py claude codex    only these
    python tests/live_providers.py --host tmux codex
                                                   on a tmux host; run this under
                                                   scripts/tmux_isolated_test.sh

Each provider is started through the listener's own handlers on the direct
host, answers one message, then answers a follow-up that only works if the
provider kept its conversation. Telegram is captured; everything else is real,
so each run spends a little of that provider's quota. The unit tests use
fixtures; this is the check that a provider actually works on this machine.

Not collected by `unittest discover`: it needs signed-in CLIs.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
import test_direct_host as fixtures  # noqa: E402

# A cheap model for each provider, and the CLI that must be installed.
LIVE = {
    "claude": ("/switch claude haiku low", "claude"),
    "codex": ("/switch codex luna low", "codex"),
    "agy": ("/switch agy g-flash", "agy"),
    "dsh": ("/switch dsh flash low", "dsh"),
}
PHRASE = "LIVE CHECK 7Q"


def scenario(root: Path, provider: str) -> None:
    from teleagent import host
    relay = fixtures.Relay(root)

    def answer(prompt: str) -> str:
        record = relay.receive(prompt)
        assert "submission confirmed" in record.get("relay_result", ""), record.get("relay_result")
        fixtures.wait_for(lambda: relay.last_event() in ("task_complete", "turn_aborted"),
                          "the provider's reply", 240)
        return " | ".join(relay.forwarded())

    with relay.patch:
        try:
            relay.receive(LIVE[provider][0])
            print("launch:", relay.sent[-1].splitlines()[0][:150], flush=True)
            try:
                fixtures.wait_for(relay.metadata, "session registration", 20)
            except AssertionError:
                pass  # The Codex TUI creates its session with the first message.
            first = answer(f"Reply with exactly this phrase and nothing else: {PHRASE}")
            print("reply:", first[:200], flush=True)
            assert PHRASE in first, "the provider did not return the requested phrase"
            second = answer("What exact phrase did I ask you to reply with in my previous "
                            "message? Answer with the phrase only.")
            print("follow-up:", second[:200], flush=True)
            assert PHRASE in second, "the provider did not keep its conversation"
            relay.receive("/status")
            assert "idle" in relay.sent[-1], relay.sent[-1]
            relay.receive("/kill_agent")
            print("stop:", relay.sent[-1].splitlines()[0][:120], flush=True)
            print(f"LIVE PASS {provider}", flush=True)
        finally:
            host.stop(relay.args.target_pane)


def environment(root: Path, host_kind: str) -> dict[str, str]:
    """An isolated bot runtime on the chosen host, with the operator's real accounts."""
    env = fixtures.relay_environment(root, None)
    env["TELEAGENT_HOST"] = host_kind
    env["TELEAGENT_TMUX_SESSION"] = "direct-test"
    # Keep the isolated tmux server that scripts/tmux_isolated_test.sh provides.
    env.update({name: value for name, value in os.environ.items() if name.startswith("TMUX")})
    # Real sign-ins live in the real homes; only the bot's runtime is temporary.
    for name in ("TELEAGENT_CODEX_HOME", "TELEAGENT_DSH_HOME"):
        env.pop(name)
    for name in ("TELEAGENT_DS_KEY_FILE", "TELEAGENT_DSH_HOME", "TELEAGENT_CLAUDE_BIN",
                 "TELEAGENT_AGY_BIN", "TELEAGENT_DSH_BIN"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def main(argv: list[str]) -> int:
    from teleagent import host
    host_kind = host.kind()
    if argv[:1] == ["--host"] and len(argv) >= 2:
        host_kind, argv = argv[1], argv[2:]
    print(f"host: {host_kind}")
    chosen = argv or list(LIVE)
    unknown = [name for name in chosen if name not in LIVE]
    if unknown:
        print(f"Unknown provider: {unknown[0]}. Choose from {', '.join(LIVE)}.")
        return 2
    results: dict[str, str] = {}
    for provider in chosen:
        print(f"\n=== {provider} ===", flush=True)
        if not shutil.which(os.environ.get(f"TELEAGENT_{provider.upper()}_BIN", LIVE[provider][1])):
            results[provider] = "not installed"
            print("not installed on this machine")
            continue
        root = Path(tempfile.mkdtemp(prefix=f"teleagent-live-{provider}-"))
        try:
            result = subprocess.run(
                [sys.executable, __file__, "--scenario", str(root), provider],
                env=environment(root, host_kind), text=True, capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            results[provider] = "FAILED (timed out)"
            continue
        finally:
            shutil.rmtree(root, ignore_errors=True)
        print(result.stdout.strip())
        passed = f"LIVE PASS {provider}" in result.stdout
        results[provider] = "passed" if passed else "FAILED"
        if not passed:
            print(result.stderr.strip()[-1500:])
    print("\n=== summary ===")
    for provider, outcome in results.items():
        print(f"{provider}: {outcome}")
    return 0 if all(outcome == "passed" for outcome in results.values()) else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        scenario(Path(sys.argv[2]), sys.argv[3])
    else:
        raise SystemExit(main(sys.argv[1:]))
