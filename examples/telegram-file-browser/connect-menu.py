"""Register this demo's current HTTPS address in the owner's bot chat."""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

def connect(repo: Path, root: Path, secret_env: Path | None = None, origin: str | None = None) -> None:
    sys.path.insert(0, str(repo / 'scripts'))
    import setup_telegram_notify as setup
    from teleagent import ui

    config = setup.parse_env_file(setup.resolve_secret_path(repo, secret_env))
    if not config.get('TELEAGENT_BOT_TOKEN') or not config.get('TELEAGENT_CHAT_ID'):
        raise SystemExit('Configure this bot and its owner private chat first.')
    if not config['TELEAGENT_CHAT_ID'].isdecimal():
        raise SystemExit('The Files launch button requires the owner private chat, not a group.')
    if origin is None:
        log = (root / '.runtime' / 'tunnel.log').read_text()
        match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', log)
        if not match:
            raise SystemExit('The HTTPS tunnel has not supplied an address yet.')
        origin = match[0]
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or any(c.isspace() or ord(c) < 32 for c in origin)):
        raise SystemExit('Supply an HTTPS origin without credentials, query or path.')
    origin = origin.rstrip('/')
    key = (root / '.runtime' / 'access-key').read_text().strip()
    if not re.fullmatch(r'[a-f0-9]{64}', key):
        raise SystemExit('Start this machine’s file service to create its private access key.')
    (root / '.runtime').chmod(0o700)
    launch_file = root / '.runtime' / 'launch-url'
    temporary = launch_file.with_suffix('.tmp')
    temporary.write_text(origin + '/?key=' + key, encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(launch_file)
    env = {'TELEAGENT_MINI_APP_URL_FILE': str(launch_file)}
    web_app = {'url': ui.mini_app_url(env)}
    if not web_app['url']:
        raise SystemExit('The Mini App needs a valid HTTPS launch address.')
    setup.telegram_api(config['TELEAGENT_BOT_TOKEN'], 'setChatMenuButton', {
        'chat_id': config['TELEAGENT_CHAT_ID'], 'menu_button': json.dumps({'type': 'commands'})
    })
    setup.telegram_api(config['TELEAGENT_BOT_TOKEN'], 'setMyCommands', {
        'commands': json.dumps(ui.command_menu(env))
    })
    binding = hashlib.sha256((config['TELEAGENT_BOT_TOKEN'] + ':' + config['TELEAGENT_CHAT_ID']).encode()).hexdigest()[:16]
    message_file = root / '.runtime' / f'launch-message-{binding}.id'
    markup = json.dumps({'inline_keyboard': [[{'text': 'Open file browser', 'web_app': web_app}]]})
    if message_file.exists():
        try:
            setup.telegram_api(config['TELEAGENT_BOT_TOKEN'], 'editMessageReplyMarkup', {
                'chat_id': config['TELEAGENT_CHAT_ID'], 'message_id': message_file.read_text().strip(), 'reply_markup': markup
            })
        except RuntimeError as error:
            if 'message is not modified' not in str(error):
                raise
    else:
        message = setup.telegram_api(config['TELEAGENT_BOT_TOKEN'], 'sendMessage', {
            'chat_id': config['TELEAGENT_CHAT_ID'], 'text': 'Files on this machine\nBrowse folders, preview images, and edit files in the configured demo folder.', 'reply_markup': markup
        })
        message_file.write_text(str(message['message_id']))
        setup.telegram_api(config['TELEAGENT_BOT_TOKEN'], 'pinChatMessage', {
            'chat_id': config['TELEAGENT_CHAT_ID'], 'message_id': message['message_id'], 'disable_notification': True
        })
    (root / '.runtime' / 'public-origin.txt').write_text(origin)
    print('The command menu is restored; Teleagent’s Files buttons open the current demo.')


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(os.environ.get('TELEAGENT_REPO', root.parents[1])))
    parser.add_argument('--secret-env', type=Path, default=os.environ.get('TELEAGENT_SECRET_ENV'))
    parser.add_argument('--origin', help='HTTPS origin of this machine’s file service; otherwise read the local tunnel log')
    args = parser.parse_args()
    connect(args.repo.resolve(), root, args.secret_env, args.origin)
