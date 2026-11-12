# Codex Telegram Bridge

Control a coding agent on your machine from Telegram. Send a task, follow its
progress, inspect waiting work, change models, and retrieve results from your
phone. Text, code, PDF/TXT/MD/HTML documents, photos and local voice transcription
are supported.

**One bot = one persistent agent and one conversation.** Private chats, groups
and forum topics use that agent. Each turn keeps the chat and topic that started
it; a message from another chat waits instead of redirecting the reply.

## Separate contexts for substantial work

The persistent tele-agent keeps the conversation and the user's decisions.
When a task benefits from a different context, it can delegate to a fresh
Codex session with an appropriate prompt, then review the result before
replying. A writing session needs the draft, relevant sources, and the author's
instructions rather than the bot's unrelated conversation history.

The included [writer profile](docs/writer.md) uses GPT-6 Astra with **high**
reasoning and prompts focused on coherent exposition. It is a normal Codex
session: only its prompt, model, and reasoning settings differ. Tools and access
follow the usual configuration. Simple requests can stay in the main
conversation; delegation does not require a separate execution framework.

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
| `/models` | Lists each provider's models with buttons: tap a provider, a model, then an effort level to switch without typing. Models are shown by the provider's own name. A provider this machine cannot start is marked on the first screen as not installed or needing sign-in; tapping one that needs sign-in offers a Sign in button. The Model button under `/status` opens the same picker. |
| `/switch PROVIDER [MODEL] [LEVEL]` | Changes provider with a fresh session. `PROVIDER` is `codex`, `claude`, `dsh` or `agy`; for example `/switch claude opus max`. Defaults: Claude Opus/medium, DeepSeek Harness Flash/max. |
| `/update` | Shows each provider CLI's installed version and the newest published one, with buttons to update one or all. Each CLI is updated by its own updater. `/status` lists the same versions in the owner's private chat; nothing is announced unasked. |
| `/usage [codex\|claude]` | Shows live usage limits in the owner's private chat. Without a provider it reports the running agent's. |
| `/auth [codex\|claude] [status\|login\|logout\|cancel]` | Sign-in controls in the owner's private chat; plain `/auth` shows status. Claude login returns a browser link; paste the one-time code it shows into the chat. Codex supports `status` and `login`. |
| `/model astra xhigh` | Inside a Codex chat, selects GPT-6 Astra and effort. Changes within a Codex home preserve the conversation. |
| `/reasoning high` | Inside a Codex chat, changes effort in the current conversation. |
| `/kill_agent` | Stops the agent persistently; Telegram controls stay online. |
| `/start_agent [MODEL] [LEVEL]` | Starts a stopped agent. |
| `/restart_agent [MODEL] [LEVEL]` | Replaces a running agent with a fresh conversation. Without a model it keeps the running agent's model and effort. |
| `/timed 0.5 check the experiment` | Schedules a task in half an hour. Use `/timed list` or `/timed remove N`. |
| `/recent_messages`, `/replay_last` | Inspects or resubmits requests from this chat. |
| `/help` | Opens the compact command guide. `/help all` lists every command. |

Earlier spellings keep working: `/claude` and `/dsh` are `/switch claude` and
`/switch dsh`; `/claude_usage` and `/codex_usage` are `/usage claude` and
`/usage codex`; `/claude_auth` is `/auth claude`; `/reauth` is `/auth login`.

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

Install `requirements.txt` in the listener's Python environment before bootstrap;
setup checks that the Markdown parser works. The shared
[Telegram chat skill](.agents/skills/telegram-chat/SKILL.md) covers replies,
formatting, files, photos, voice messages and size limits for agents on each host.

The listener controls Codex; Codex connects to the model provider. Available
models depend on that installation and account. The included aliases are Astra,
Sol, Luna, Spark, DeepSeek Flash and DeepSeek Pro. `ds-flash` and `ds-pro` keep
using the existing Codex harness with the DeepSeek provider. `/dsh [flash|pro]`
starts the official native DeepSeek Harness instead and begins a fresh session.

## Run without tmux: Windows, macOS or any host

The relay also runs where bash and tmux are unavailable. It then starts and
supervises the agent process itself instead of placing it in a tmux pane.

```bash
python scripts/relay.py start    # start whatever is not running, in the background
python scripts/relay.py status
python scripts/relay.py stop     # listener, agent and file browser
python scripts/relay.py run      # listener in this terminal
python scripts/relay.py autostart on   # Windows: keep it running
```

`start` only starts what is missing, so it is safe to repeat. On Windows,
`autostart on` adds a Task Scheduler entry that runs it at sign-in and every
five minutes: the counterpart of the cron watchdog on Linux. It also makes the
bot independent of the terminal or program that first started it; without it,
a bot started from another program's session can stop when that session ends.
`autostart off` removes the entry.

With `TELEAGENT_FILES_TUNNEL="cloudflare"` in `config/relay.env`, `start` also
keeps the [file browser](examples/telegram-file-browser/README.md) and a
Cloudflare quick tunnel running, and registers the Files button again whenever
the tunnel is issued a new address. It needs `node` and `cloudflared` on PATH.

It needs Python 3.11 or newer with `requirements.txt` installed, the same
`config/relay.env` and secret file as any other host, and the signed-in CLI of
each provider you want to use. Every provider runs in its own program: Claude
Code through `claude`, Codex through `codex exec`, Antigravity through `agy`
and DeepSeek Harness through `dsh`. Set `TELEAGENT_CODEX_MODEL` in
`config/relay.env` to the default agent, for example `cc-opus`.

What differs from a tmux host: Codex runs without its terminal interface, so
it has no live `/model` or `/reasoning` change and no Goal mode (use `/switch`
for a fresh session). On Windows, DeepSeek through Codex (`ds-flash`,
`ds-pro`) and named bots that need a prepared Codex home are unavailable,
because shell scripts prepare those; and Claude sign-in from Telegram
(`/auth claude login`) needs a Unix host, so sign in with the CLI instead.

DeepSeek Harness needs a current Node.js (22.20 works); on an older one the
relay says so when you switch to it. With `TELEAGENT_DS_KEY_FILE` set it runs
in its own isolated home with that key; without it, a host without tmux lets
the harness use its own home and sign-in.

A host with tmux uses it by default; `TELEAGENT_HOST=direct` or
`TELEAGENT_HOST=tmux` chooses explicitly. See [architecture](docs/architecture.md).

## Check a machine with the real providers

The automated tests use small stand-in programs in place of each provider's
CLI, so they prove the relay, not the provider. To check that a provider really
works on a machine:

```bash
python tests/live_providers.py                # every installed provider
python tests/live_providers.py claude codex   # only these
python tests/live_providers.py codex=sol      # a particular model
scripts/tmux_isolated_test.sh -- python3 tests/live_providers.py --host tmux
```

The last form checks a tmux host on its own private tmux server, including
Codex with its terminal interface.

It starts each provider through the relay's own handlers, sends one message,
then a follow-up that only succeeds if the provider kept its conversation.
Telegram is not contacted; each run spends a little of that provider's quota.

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
# After reviewing, testing, committing and pushing the changes:
scripts/deploy_listener.sh
```

The check script uses an isolated tmux server. Deployment restarts only this
instance's listener and verifies that the Codex pane identity stays unchanged.
Queues, reply offsets, credentials and the agent conversation are retained.

See [architecture and recovery](docs/architecture.md),
[operator controls](docs/telegram_codex_agent.md),
[notifications and files](docs/notifications.md), and
[tmux test safety](docs/tmux_safety.md). MIT licensed; see [LICENSE](LICENSE).
