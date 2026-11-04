# Telegram file-browser demo

This Mini App browses the example's `demo-files/` folder, previews images, and edits UTF-8 text files inside Telegram. The existing bot handles `/files`; a separate Node service reads and writes files on the configured machine. Opening or saving a file does not require a model call.

File access is disabled unless the operator or an authorized agent configures it for that machine and bot. Pulling this repository does not start a file service, publish a URL, or enable Files. Never reuse another machine's private URL, key or `.runtime/` directory. Named bots do not inherit the main bot's Files setting.

## Configure this machine and bot

Requires Node.js 18+ and the working Python/Telegram relay. No npm packages are needed. Run the following from the repository root, on the machine being configured:

1. Start `node examples/telegram-file-browser/server.cjs`. It listens on `127.0.0.1:8766`; `DEMO_PORT` overrides the port. The service creates a random local access key. Keep it running.
2. Route an HTTPS origin to that loopback address with your reverse proxy or tunnel. Do not expose the loopback port directly. The origin must serve the app at `/`.
3. Add the following setting to this bot's ignored `config/relay.env`, or `config/relay-<instance>.env` for a named bot. The path must be readable from the listener's environment:

   ```bash
   export TELEAGENT_MINI_APP_URL_FILE="$TELEAGENT_REPO/examples/telegram-file-browser/.runtime/launch-url"
   ```

4. Load the selected bot's environment and register its launch button:

   ```bash
   # Set TELEAGENT_INSTANCE first if this is a named bot.
   source scripts/relay_paths.sh
   python3 examples/telegram-file-browser/connect-menu.py --origin https://files.example.com
   ```

   Replace the example origin with this service's HTTPS origin. The connector reads the selected `TELEAGENT_SECRET_ENV`; `--secret-env` and `--repo` allow explicit paths. Credentials are read from the local secret file, never command arguments. Without `--origin`, it reads a local Cloudflare temporary address from `.runtime/tunnel.log`.

5. Restart only this bot's listener using `scripts/deploy_listener.sh`, then request `/status` in its owner's private chat. The normal command menu and Refresh, Queue and Help remain; Files is added alongside them. `/files` and the pinned **Open file browser** button also launch the app.

Rerun the connector when the HTTPS address changes. Fresh control replies read the updated URL without a listener restart. Existing messages may retain an old URL; use `/files` to get a current button. The machine must remain awake and online. Use separate example directories, ports and private runtime state for separate file services.

To disable Files, set `export TELEAGENT_MINI_APP_URL_FILE=""` in this bot's ignored configuration and restart its listener; stop the file service/tunnel to invalidate access through previously issued buttons. An explicit empty setting also clears a value inherited from the current shell. Do not commit runtime files or copy them to another machine.

## Access boundary

Only `demo-files/` is exposed. Hidden files, linked folders and paths outside it are rejected. Edits keep a private backup under `.runtime/backups/` and reject stale revisions. The sample notes, CSV and plot are synthetic.

This demonstration authenticates with a private bearer key in the launch URL, not Telegram user identity. Anyone who obtains that URL can read and edit the demo folder. Keep the launch URL and generated `Open demo.html` private; never post them in a group, log or shared artifact. The relay sends launch buttons only to its configured owner's private chat. Do not put sensitive files in this demonstration. A deployment for sensitive files needs server verification of Telegram `initData`, freshness and user/action authorization.

Run checks with `node --test examples/telegram-file-browser/test.cjs` and `python3 -m unittest discover -s tests -p test_telegram_mini_app.py`. CI runs both. The server uses Node's standard library; Telegram supplies the client-side Mini App bridge.

Scope: folder navigation, image previews, text editing, saving and small downloads. Uploads, deletion, renaming and a full IDE are not implemented. External editors do not share the save lock; coordinate concurrent writes before using this for active project files.
