# Codex Telegram Bridge

Control a persistent agent on your machine from Telegram. Send a task, follow its
progress, inspect waiting work, change models, and retrieve results from your
phone. Text, code, PDF/TXT/MD/HTML documents, photos and local voice transcription
are supported.

**One bot = one persistent agent and one conversation.** Private chats, groups
and forum topics use that agent. Each turn keeps the chat and topic that started
it; a message from another chat waits instead of redirecting the reply.

## A persistent coordinator, temporary specialists

The bot keeps the conversation: your intent, approved decisions, and the place
where results should return. For substantial tasks it can delegate a bounded
assignment to a specialist with its own instructions and a fresh context. A
writer needs the draft and the author's brief; it does not need the bot's
unrelated debugging history or terse Telegram personality.

The handoff matters as much as the role. Preserve the user's relevant words,
separate approved decisions from the coordinator's suggestions, and supply
selected source material. The specialist returns the actual artifact, sources
used, and unresolved issues. The coordinator reviews that work and responds in
the original conversation. A completion message alone is not a useful result.

The wrapper includes a tool-free **writer** using **GPT-6 Astra / high** for
exposition and substantive editing. Each run saves its input snapshots and
report in a private directory belonging to the bot instance. It uses a separate
Codex home and an explicit model, with no fallback retry. Ordinary questions
can still be answered directly. Full-access bots receive the delegation guide
on their next managed launch; chat-only bots retain their tool restrictions.

For example, ask: “Use the writer specialist to draft from this approved
outline and these sources, then review the result against my instructions.”
See [specialist roles and handoffs](docs/specialists.md) for the runner, role
configuration, and current limits. Specialist outputs are never automatically
sent to Telegram or published.

## Use it from your phone

Send ordinary instructions:

> In ~/project, reproduce the failing test, fix it, and show me the diff.

> Run the experiment in tmux, save the results, and report when it finishes.

> Compare the last two runs and send the reviewed report.md.

The agent runs on the host with the access configured for this bot. Reach other
machines through the host's existing SSH or scheduler setup. Nothing needs a
new Telegram-specific command just to become a task.

| Telegram action | What happens |
| --- | --- |
| `/status` | Shows working/idle/stopped/recovering, effective model, queue counts and reply-check age. Tap Refresh to check again. |
| `/queue` | Shows waiting work, pending receipts and delivery problems. |
| `/cancel ID` | Removes a waiting message. `/cancel all` removes the queue visible here. |
| `/interrupt NEW TASK` | Aborts the managed turn, verifies it stopped, then submits the replacement instruction. |
| `/models` | Lists model names and example commands. |
| `/model astra xhigh` | Selects GPT-6 Astra and effort. Changes within a Codex home preserve the conversation. |
| `/reasoning high` | Changes effort in the current conversation. |
| `/kill_agent` | Stops the agent persistently; Telegram controls stay online. |
| `/start_agent [MODEL] [LEVEL]` | Starts a stopped agent. |
| `/restart_agent [MODEL] [LEVEL]` | Replaces a running agent with a fresh conversation. |
| `/timed 0.5 check the experiment` | Schedules a task in half an hour. Use `/timed list` or `/timed remove N`. |
| `/recent_messages`, `/replay_last` | Inspects or resubmits requests from this chat. |
| `/reauth`, `/codex_usage` | Handles sign-in and account usage in private. |
| `/help` | Opens the compact command guide. |

Lifecycle commands take model/effort options, never task text. Send the task in
a separate message. Changing between provider homes starts a fresh conversation.

If Telegram cannot deliver a control result, the relay saves and retries that
reply without repeating the action. `/status` shows delivery failures; `/queue`
shows waiting work and any delivery problems with their dates and reasons.
A missing receipt does not establish that the task failed.

## Groups and private messages

Add the bot to a group. It discovers its username through Telegram and uses
the configured private chat to identify its owner. Optional identity overrides
are `TELEAGENT_OWNER_USER_ID` and `TELEAGENT_BOT_USERNAME`. Address the exact bot
username, use `/status@YourBot`, or reply to a message from that bot. Captions
and forum topics work too. Other bots with similar names are ignored.

The owner can use agent controls in a group. Other members can submit addressed
requests when the configured owner is confirmed as a group member; lifecycle,
model and queue mutation controls remain owner-only. Telegram may require the
bot to be a group administrator for reliable membership checks.

A same-chat message can steer an ordinary active turn. Other chats wait in the
single FIFO. Goal-mode work is never automatically interrupted to make room.
Group replies are concise finals; account recovery and unscoped machine notices
go privately. Group status and replay do not expose private message history.
If inputs from different chats nevertheless enter one turn, remaining output
is redirected to the owner's private chat.

This is destination isolation within a shared agent. The agent retains one
conversation's context across chats; separate bot instances provide separate
agent histories when that is needed.

## Install on a new machine

The guided bootstrap supports Linux x86_64 and arm64. Have a Telegram bot token
from [BotFather](https://t.me/BotFather) and a DeepSeek API key ready.

1. Open your bot in Telegram and send `/start`.
2. Clone this repository and run `scripts/bootstrap_new_machine.sh`.
3. Enter the hidden credentials and choose the intended private chat.
4. Send `/ping`, then a normal task.

The bootstrap checks prerequisites, installs a compatible standalone Codex CLI,
configures DeepSeek V4 Flash, starts the supervised agent/listener and installs
a crontab watchdog when available. It does not require Node.js. For an existing
OpenAI-authenticated Codex installation, follow the [operator setup](docs/telegram_codex_agent.md).

The listener controls Codex; Codex connects to the model provider. Available
models depend on that installation and account. The included aliases are Astra,
Sol, Luna, Spark, DeepSeek Flash and DeepSeek Pro.

## Private host configuration

Copy `config/relay.env.template` to ignored `config/relay.env` for local options.
Each additional bot uses `TELEAGENT_INSTANCE=name`, its own secret file, runtime
directory, Codex home and tmux session. See [instance setup](docs/telegram_codex_agent.md).

Telegram credentials live in `.secrets/notify.env` (or the instance-specific
secret file); bootstrap stores DeepSeek credentials in
`~/.config/tele-agent/deepseek.env`. These are private mode-0600 files. Local
personalities belong in ignored `config/personality.md` or
`config/personality-<instance>.md`; the tracked default is neutral.

## Development and deployment

```bash
scripts/check.sh
# After reviewing and committing the changes:
scripts/deploy_listener.sh
```

The check script uses an isolated tmux server. Deployment restarts only this
instance's listener and verifies that the Codex pane identity stays unchanged.
Queues, reply offsets, credentials and the agent conversation are retained.

See [architecture and recovery](docs/architecture.md),
[operator controls](docs/telegram_codex_agent.md),
[notifications and files](docs/notifications.md), and
[tmux test safety](docs/tmux_safety.md). MIT licensed; see [LICENSE](LICENSE).
