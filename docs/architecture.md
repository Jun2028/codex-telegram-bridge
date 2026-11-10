# Relay architecture and recovery

The runtime owns one Codex agent per bot. Splitting services does not create
additional agents, conversations or per-chat workers.

```mermaid
flowchart LR
  Telegram --> Poller
  Poller --> Quick[Status / help / queue]
  Poller --> Inbox[Durable inbox]
  Inbox --> Control[Serial control worker]
  Control --> FIFO[Waiting task FIFO]
  Control --> Codex[One supervised Codex agent]
  FIFO --> Codex
  Codex --> Events[Session JSONL]
  Events --> Delivery[Independent reply worker]
  Delivery --> Origin[Bound originating chat / topic]
```

## Components

`scripts/telegram_inbox.py` is the compatible CLI/import entry point. The
implementation is the `scripts/teleagent` package; tests mock the module that
owns a dependency rather than a global in the old monolith.

| Component | Responsibility |
| --- | --- |
| `app`, `service` | Runtime assembly, poller, durable ingress, one serial control worker and independent outbound worker |
| `ui`, `commands` | Fast phone controls and agent/account operations |
| `routing`, `events`, `delivery` | Authorization, immutable prompt destinations, turn binding, normalization, deduplication and reply cursors |
| `replies` | Durable control-result delivery, retry backoff and independent progress for each chat/topic |
| `queue`, `submission` | FIFO, exact root-user submission checks and bounded composer recovery |
| `lifecycle`, `processes`, `sessions` | Desired lifecycle state, tmux/process identity, valid root-session discovery |
| `harnesses` | The one table of agent harnesses: label, pane process name, launcher variable and which controls apply |
| `models`, `status` | Model selectors and observed agent state |
| `auth`, `usage` | Authentication, account recovery and rate-limit event tracking |
| `attachments`, `messages`, `identity`, `schedule` | Downloads, message formatting, scoped replay and timed work |
| `state`, `transport`, `settings` | Atomic private JSON, locks, Telegram API and configuration |

## Hosts

The agent runs in one of two hosts, chosen by `teleagent/host.py`:

| Host | Where | How the agent runs |
| --- | --- | --- |
| `tmux` | Linux with tmux | In a tmux pane under `codex_agent_supervisor.sh`; required for the Codex TUI |
| `direct` | Anywhere else, including Windows and macOS | `teleagent/direct_pane.py` owns the runner, sends it the keystrokes tmux would, restarts it after a crash and keeps its output for `/status` |

The listener is the same in both. Its low-level questions (is an agent
running, which harness, which session log, send these keys) go through
`processes.py` and `telegram_agent_registry.py`, which ask tmux and `/proc` on
a tmux host and the direct pane's own record on a direct host.
`teleagent/portable.py` holds the remaining operating-system differences: file
locks, process checks and stopping a process tree.

`scripts/relay.py` starts, stops and supervises the listener without bash.
`teleagent/environment.py` computes the settings `relay_paths.sh` exports;
`tests/test_environment.py` compares the two. `tests/test_direct_host.py` runs
the listener against a direct-host agent for every provider and avoids every
Unix-only call, so it runs on Windows and macOS as well as Linux.

The direct host runs every provider through its runner. Codex runs there
through `codex_exec_runner.py`, which drives `codex exec`, because its terminal
interface needs a terminal; on a tmux host Codex keeps its terminal interface,
with live model changes and Goal mode.

## Runners

A runner keeps one provider conversation alive. `teleagent/runner.py` holds
everything runners share: the session log the listener reads, the input
protocol, the serve loop and interruption. A provider's runner
(`claude_agent_runner.py`, `codex_exec_runner.py`, `agy_agent_runner.py`,
`dsh_agent_runner.py`) subclasses `AgentRunner` and supplies one method,
`run_message`, which runs a single CLI turn and writes the answer.

The input protocol is the same under tmux and under the direct host: a
bracketed paste carries the message, Enter submits it, Escape interrupts the
running turn, and Ctrl-U discards unsent text.

`tests/live_providers.py` runs each provider's real CLI through the relay. The
unit tests use stand-in CLIs, so run it on a machine to confirm a provider
works there.

## Harnesses and commands

A harness is the program in the agent pane: the Codex TUI, or a runner that
drives another provider's CLI (`claude_agent_runner.py`, `dsh_agent_runner.py`,
`agy_agent_runner.py`). Every runner writes the same session JSONL, so
submission checks, delivery and status do not depend on the provider.

`teleagent/harnesses.py` is the single description of each harness. To add a
provider:

1. Add its entry to `harnesses.py`, with its runner script and session file.
2. Write its runner: subclass `AgentRunner` and implement `run_message`.
3. Add its launch arguments to `lifecycle.direct_runner_command`, and its
   models to `provider_commands.CHOICES` so `/switch` and the picker offer it.
4. For tmux hosts, teach `relay_paths.sh`, `codex_agent_supervisor.sh` and
   `start_codex_agent.sh` its launch models and process name. Shell cannot
   import the table, so `tests/test_harness_registry.py` fails when the two
   disagree.
5. Add a stand-in CLI to `tests/test_direct_host.py` and an entry to
   `tests/live_providers.py`.
6. Add its files to `config/public-export.txt` when they are portable.

Harness-specific launch validation still lives in `lifecycle.start_codex_agent`
and `messages.parse_agent_launch_payload`.

`teleagent/provider_commands.py` resolves `/switch`, `/usage` and `/auth`, which
take the provider as an argument; a new harness joins them through its table
entry instead of a new command.

`ui.COMMANDS` is the single list of Telegram commands. The command menu,
`/help all` and the unknown-command suggestion are generated from it, and the
same test fails when a handler and the table differ.

## Delivery invariants

1. Every accepted normal update is stored before the Telegram offset advances.
   A single runtime lock prevents duplicate pollers sharing the same state.
2. Exactly one worker writes to the agent composer or runs lifecycle controls.
   Status and reply delivery do not wait behind a slow model/account operation.
3. The route record is reserved before the prompt is pasted. Reusing a route ID
   cannot change its chat or topic.
4. A real Codex turn binds to its first trusted Telegram route. Later input from
   a different destination quarantines the remaining output to the owner's PM.
   Unknown routes also resolve privately; the mutable latest-chat hint cannot
   authorize a group destination.
5. Only assistant messages are forwarded. Alternate Codex event schemas are
   deduplicated by message ID and by content/phase within a turn. The turn binding
   and recent keys survive listener restart.
6. Incomplete JSONL lines wait for a later read. Failed sends retain the cursor.
   A crash between Telegram accepting a send and the local cursor commit can
   duplicate a reply: the Bot API does not provide an exactly-once transaction.
7. Group replay reads group-scoped files even if legacy private-log splitting is
   disabled. Status never includes an assistant-history preview. Group prompts
   do not inject the private per-person memory summary.
8. An uncertain inbound delivery is archived for review instead of blindly
   pasted again. Accepted but unstarted ingress survives restart in FIFO order,
   but queued agent work is bound to the accepting agent; an explicit restart
   quarantines it instead of relaying it to the replacement agent.
9. Timed work reserves its original chat/topic and waits while the agent is busy.
   The queue never presses Escape or resumes a goal automatically.
10. A failed control-result send retries the saved reply without repeating its
    command. Failure in one destination does not block control replies to other
    chats. Partial control replies retain only their unsent chunks.
11. Delivery and maintenance failures remain visible in status until a successful
    check. Recovery failures produce a rate-limited private notice. Corrupt or
    unreadable state raises an error instead of being overwritten as empty.

State mutations use file/thread locks and atomic replacement with mode 0600 and
fsync. Each state file has one primary writer; route and queue mutations that
can overlap with phone controls use shared locks.

## Runtime files

All paths are relative to the instance's `TELEAGENT_LOG_DIR`.

| File | Purpose |
| --- | --- |
| `telegram_inbox.offset` | Last Telegram update acknowledged |
| `telegram_ingress.state.json` | Accepted incoming updates and interrupted delivery archive |
| `telegram_relay_queue.state.json` | Waiting, failed and cancelled tasks |
| `telegram_relay_confirmation.state.json` | Submitted messages awaiting a root-user event |
| `telegram_reply_route.state.json` | Prompt-to-chat/topic destination records |
| `telegram_agent_messages.state.json` | Reply cursor, active turn destination and deduplication keys |
| `telegram_health.state.json` | Control/delivery heartbeat and current control-operation age |
| `telegram_control_replies.state.json` | Pending control results and notices, with immutable destinations and retry state |
| `telegram_agent_lifecycle.state.json` | Operator's persistent running/stopped preference |
| `telegram_timed_messages.state.json` | Scheduled tasks and their delivery state |

Do not delete offsets or queues to repair a stalled bot: that can replay old
work. Use `/status` and `/queue`, inspect the exact instance's local logs, and
restart only the failed listener when its agent remains healthy. A deliberately
stopped agent stays stopped until `/start_agent`.

## Validation

`scripts/check.sh` runs the behavioral suite under a private tmux socket and
checks shell syntax. Regression coverage includes mixed PM/group turns, real
turn boundaries, forum topics, restart during delivery, duplicate schemas,
mention/reply matching, callback authorization, group-safe queue controls,
concurrent state writes, a blocked control worker with live outbound delivery,
model/account recovery and per-instance process isolation.
