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
    from teleagent import host, processes
    relay = fixtures.Relay(root)

    def answer(prompt: str) -> str:
        record = relay.receive(prompt)
        assert "submission confirmed" in record.get("relay_result", ""), record.get("relay_result")
        fixtures.wait_for(lambda: relay.last_event() in ("task_complete", "turn_aborted"),
                          "the provider's reply", 240)
        return " | ".join(relay.forwarded())

    with relay.patch:
        try:
            relay.receive(os.environ.get("TELEAGENT_LIVE_LAUNCH") or LIVE[provider][0])
            print("launch:", relay.sent[-1].splitlines()[0][:150], flush=True)
            assert "started in" in relay.sent[-1], "the agent did not start"
            if provider == "codex" and not host.is_direct():
                # The Codex TUI creates its session with the first message.
                fixtures.wait_for(lambda: processes.codex_tui_idle(relay.args.target_pane),
                                  "the Codex composer", 120)
            else:
                fixtures.wait_for(relay.metadata, "the runner's session log", 120)
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
        except BaseException:
            # What the agent's screen and supervisor showed, for diagnosis.
            try:
                print("--- agent screen ---\n" + processes.tmux_tail(relay.args.target_pane, 200).strip()[-1500:])
                pane = relay.registry.tmux_pane_pid(relay.args.target_pane)
                rows = relay.registry.ps_rows()
                tree = set(relay.registry.descendants(pane)) if pane else set()
                print("--- agent processes ---", [name for pid, _parent, name in rows if pid in tree])
            except Exception as exc:  # noqa: BLE001
                print(f"--- agent screen unavailable: {type(exc).__name__}: {exc}")
            log = relay.runtime / "codex_agent.supervisor.log"
            if log.is_file():
                print("--- supervisor log ---\n" + log.read_text(errors="replace")[-800:])
            raise
        finally:
            if host.is_direct():
                host.stop(relay.args.target_pane)


def environment(root: Path, host_kind: str) -> dict[str, str]:
    """An isolated bot runtime on the chosen host, with the operator's real accounts."""
    env = fixtures.relay_environment(root, None)
    env["TELEAGENT_HOST"] = host_kind
    env["TELEAGENT_TMUX_SESSION"] = "direct-test"
    if os.environ.get("TELEAGENT_LIVE_LAUNCH"):
        env["TELEAGENT_LIVE_LAUNCH"] = os.environ["TELEAGENT_LIVE_LAUNCH"]
    # Keep the isolated tmux server that scripts/tmux_isolated_test.sh provides.
    env.update({name: value for name, value in os.environ.items() if name.startswith("TMUX")})
    # Real sign-ins live in the real homes; only the bot's runtime is temporary.
    for name in ("TELEAGENT_CODEX_HOME", "TELEAGENT_DSH_HOME"):
        env.pop(name)
    for name in ("TELEAGENT_CODEX_HOME", "TELEAGENT_DS_KEY_FILE", "TELEAGENT_DSH_HOME",
                 "TELEAGENT_CLAUDE_BIN", "TELEAGENT_AGY_BIN", "TELEAGENT_DSH_BIN"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def main(argv: list[str]) -> int:
    from teleagent import host
    host_kind = host.kind()
    if argv[:1] == ["--host"] and len(argv) >= 2:
        host_kind, argv = argv[1], argv[2:]
    print(f"host: {host_kind}")
    # `provider=model` checks another model, for example `codex=sol`.
    requested = dict(item.partition("=")[::2] for item in (argv or list(LIVE)))
    unknown = [name for name in requested if name not in LIVE]
    if unknown:
        print(f"Unknown provider: {unknown[0]}. Choose from {', '.join(LIVE)}.")
        return 2
    results: dict[str, str] = {}
    for provider, model in requested.items():
        print(f"\n=== {provider} ===", flush=True)
        if model:
            os.environ["TELEAGENT_LIVE_LAUNCH"] = f"/switch {provider} {model}"
        else:
            os.environ.pop("TELEAGENT_LIVE_LAUNCH", None)
        # The relay installs the DeepSeek Harness itself on first use.
        installs_itself = provider == "dsh" and os.name != "nt"
        if not installs_itself and not shutil.which(
                os.environ.get(f"TELEAGENT_{provider.upper()}_BIN", LIVE[provider][1])):
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
            if host_kind == host.TMUX and os.environ.get("TMUX_TMPDIR"):
                # A tmux server keeps its first environment; end this provider's
                # session so the next one starts a server with its own.
                subprocess.run(["tmux", "kill-session", "-t", "direct-test"],
                               check=False, capture_output=True)
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
