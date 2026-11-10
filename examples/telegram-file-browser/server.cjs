const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');

const MAX_TEXT = 1024 * 1024;
const MAX_FILE = 10 * 1024 * 1024;
const textExtensions = new Set(['.txt', '.md', '.json', '.csv', '.tsv', '.py', '.js', '.cjs', '.ts', '.css', '.html', '.yaml', '.yml', '.toml', '.log', '.svg']);
const imageTypes = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif' };
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const fail = (status, message) => Object.assign(new Error(message), { status });

// Telegram signs the data it gives a Mini App with the bot's token. Checking
// that signature proves who opened the page, which a secret address cannot.
function telegramUser(initData, botToken, maxAgeSeconds) {
  const fields = new URLSearchParams(typeof initData === 'string' ? initData : '');
  const hash = fields.get('hash') || '';
  fields.delete('hash');
  const check = [...fields.entries()].map(([name, value]) => name + '=' + value).sort().join('\n');
  const secret = crypto.createHmac('sha256', 'WebAppData').update(botToken).digest();
  const expected = crypto.createHmac('sha256', secret).update(check).digest();
  const supplied = /^[a-f0-9]{64}$/.test(hash) ? Buffer.from(hash, 'hex') : Buffer.alloc(0);
  if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) return null;
  const age = Date.now() / 1000 - Number(fields.get('auth_date'));
  if (!(age >= -300 && age <= maxAgeSeconds)) return null;
  try { return String(JSON.parse(fields.get('user')).id); } catch { return null; }
}

async function createApp({ root, key, backupDir, readOnly = false, telegram = null }) {
  root = await fs.realpath(root);
  const sign = text => crypto.createHmac('sha256', key).update(text).digest('hex');
  // A download ticket lets Telegram's own downloader fetch one file for two
  // minutes without carrying the access key or the sign-in data in its address.
  const ticketFor = (relative, expires) => expires + '.' + sign('download\n' + relative + '\n' + expires);
  function validTicket(relative, ticket) {
    const [expires, mac] = String(ticket || '').split('.');
    if (!/^\d{1,16}$/.test(expires || '') || Number(expires) < Date.now()) return false;
    const expected = Buffer.from(ticketFor(relative, expires)), supplied = Buffer.from(expires + '.' + mac);
    return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
  }
  let writes = Promise.resolve();
  async function resolveFile(relative = '') {
    if (typeof relative !== 'string' || relative.length > 500 || /[\\\x00-\x1f:]/.test(relative) || relative.startsWith('/')) throw fail(400, 'Invalid file path.');
    const segments = relative ? relative.split('/') : [];
    if (segments.some(part => !part || part === '.' || part === '..' || part.startsWith('.') || /[ .]$/.test(part))) throw fail(403, 'This path is outside the shared folder.');
    let target = root;
    for (const part of segments) {
      target = path.join(target, part);
      if ((await fs.lstat(target)).isSymbolicLink()) throw fail(403, 'Linked folders and files are not exposed.');
    }
    const real = await fs.realpath(target);
    const within = path.relative(root, real);
    if (within === '..' || within.startsWith('..' + path.sep) || path.isAbsolute(within)) throw fail(403, 'This path is outside the shared folder.');
    return { target: real, stat: await fs.stat(real) };
  }
  function json(res, status, value) {
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(value));
  }
  async function readText(target, stat) {
    if (!stat.isFile()) throw fail(400, 'Select a file.');
    if (stat.size > MAX_TEXT) throw fail(413, 'The demo editor supports text files up to 1 MB.');
    if (!textExtensions.has(path.extname(target).toLowerCase())) throw fail(415, 'This file type is available for preview or download.');
    const bytes = await fs.readFile(target);
    let content;
    try { content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes); }
    catch { throw fail(415, 'This file is not UTF-8 text. Download it to use another editor.'); }
    if (content.includes('\0')) throw fail(415, 'Binary files cannot be edited here.');
    return { content, revision: digest(bytes) };
  }
  return http.createServer(async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org");
    try {
      const url = new URL(req.url, 'http://localhost');
      if (url.pathname === '/health') return json(res, 200, { ok: true });
      const assets = { '/': ['index.html', 'text/html; charset=utf-8'], '/app.js': ['app.js', 'text/javascript; charset=utf-8'], '/style.css': ['style.css', 'text/css; charset=utf-8'] };
      if (req.method === 'GET' && assets[url.pathname]) {
        const [name, type] = assets[url.pathname];
        res.writeHead(200, { 'Content-Type': type });
        return res.end(await fs.readFile(path.join(__dirname, 'public', name)));
      }
      if (url.pathname === '/favicon.ico') { res.writeHead(204); return res.end(); }
      if (!url.pathname.startsWith('/api/')) throw fail(404, 'Not found.');
      const relative = url.searchParams.get('path') || '';
      // Telegram's native downloader cannot attach the page's headers, so a
      // download may instead present a short-lived ticket for that one file.
      const isDownload = req.method === 'GET' && url.pathname === '/api/download';
      if (!(isDownload && validTicket(relative, url.searchParams.get('ticket')))) {
        const downloadAuth = isDownload && !telegram ? 'Bearer ' + url.searchParams.get('key') : '';
        const supplied = Buffer.from(req.headers.authorization || downloadAuth);
        const expected = Buffer.from('Bearer ' + key);
        if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) throw fail(401, 'Open the private link from the bot to connect to this folder.');
        if (telegram && telegramUser(req.headers['x-telegram-init-data'], telegram.botToken, telegram.maxAgeSeconds) !== String(telegram.ownerId)) {
          throw fail(401, 'Open this from the bot owner’s Telegram account. If it has been open for a long time, close and reopen it.');
        }
      }
      const { target, stat } = await resolveFile(relative);
      if (req.method === 'GET' && url.pathname === '/api/list') {
        if (!stat.isDirectory()) throw fail(400, 'Select a folder.');
        const entries = [];
        for (const item of await fs.readdir(target, { withFileTypes: true })) {
          if (item.name.startsWith('.') || item.isSymbolicLink() || (!item.isFile() && !item.isDirectory())) continue;
          const info = await fs.stat(path.join(target, item.name));
          entries.push({ name: item.name, kind: item.isDirectory() ? 'folder' : 'file', size: info.size, modified: info.mtime.toISOString() });
        }
        entries.sort((a, b) => (a.kind === b.kind ? 0 : a.kind === 'folder' ? -1 : 1) || a.name.localeCompare(b.name));
        return json(res, 200, { host: os.hostname(), root: path.basename(root), path: relative, entries, readOnly });
      }
      if (req.method === 'GET' && url.pathname === '/api/file') {
        if (!stat.isFile()) throw fail(400, 'Select a file.');
        if (stat.size > MAX_FILE) throw fail(413, 'Preview is limited to files up to 10 MB.');
        const type = imageTypes[path.extname(target).toLowerCase()];
        if (type) return json(res, 200, { kind: 'image', type, data: (await fs.readFile(target)).toString('base64'), size: stat.size });
        if (!textExtensions.has(path.extname(target).toLowerCase())) return json(res, 200, { kind: 'download', size: stat.size });
        return json(res, 200, { kind: 'text', ...(await readText(target, stat)), size: stat.size });
      }
      if (req.method === 'GET' && url.pathname === '/api/ticket') {
        if (!stat.isFile()) throw fail(400, 'Select a file.');
        return json(res, 200, { ticket: ticketFor(relative, String(Date.now() + 120000)) });
      }
      if (req.method === 'GET' && url.pathname === '/api/download') {
        if (!stat.isFile()) throw fail(400, 'Select a file.');
        if (stat.size > MAX_FILE) throw fail(413, 'Downloads are limited to files up to 10 MB.');
        res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': "attachment; filename*=UTF-8''" + encodeURIComponent(path.basename(target)) });
        return res.end(await fs.readFile(target));
      }
      if (req.method === 'PUT' && url.pathname === '/api/file') {
        if (readOnly) throw fail(403, 'This folder is shared read-only.');
        if (!req.headers['content-type']?.startsWith('application/json')) throw fail(415, 'Send JSON.');
        const chunks = []; let length = 0;
        for await (const chunk of req) { length += chunk.length; if (length > MAX_TEXT * 6 + 1024) throw fail(413, 'Text is too large.'); chunks.push(chunk); }
        let body;
        try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw fail(400, 'Invalid JSON.'); }
        if (!body || typeof body.content !== 'string' || !/^[a-f0-9]{64}$/.test(body.revision || '') || body.content.includes('\0')) throw fail(400, 'A text file and its original revision are required.');
        if (Buffer.byteLength(body.content, 'utf8') > MAX_TEXT) throw fail(413, 'The demo editor supports text files up to 1 MB.');
        const save = writes.then(async () => {
          const fresh = await resolveFile(relative);
          const current = await readText(fresh.target, fresh.stat);
          if (current.revision !== body.revision) throw fail(409, 'This file changed on the machine. Your edit is still here. Copy it before reloading the newer file.');
          await fs.mkdir(backupDir, { recursive: true, mode: 0o700 });
          await fs.chmod(backupDir, 0o700);
          await fs.writeFile(path.join(backupDir, digest(relative) + '-' + current.revision + '.txt'), current.content, { flag: 'w', mode: 0o600 });
          const temporary = path.join(path.dirname(fresh.target), '.save-' + crypto.randomUUID());
          try {
            await fs.writeFile(temporary, body.content, { flag: 'wx', mode: fresh.stat.mode });
            // shortcut: external writers do not share this lock; use a shared file service for simultaneous agent/editor saves.
            if (digest(await fs.readFile(fresh.target)) !== current.revision) throw fail(409, 'The file changed while saving. Your edit has not overwritten it.');
            await fs.rename(temporary, fresh.target);
          } finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
          return { revision: digest(body.content), savedAt: new Date().toISOString() };
        });
        writes = save.catch(() => {});
        return json(res, 200, await save);
      }
      throw fail(405, 'Unsupported operation.');
    } catch (error) {
      const status = error.status || (error.code === 'ENOENT' ? 404 : error.code === 'EACCES' ? 403 : 500);
      if (!res.headersSent) json(res, status, { error: status === 500 ? 'The file operation failed. Please refresh and try again.' : error.message });
      else res.end();
    }
  });
}

// Which folder is shared, and how. The sample folder needs only the private
// link. Any other folder is read-only unless TELEAGENT_FILES_WRITABLE=1, and is
// served only to the bot owner's own Telegram account.
async function sharedFolder() {
  const demo = path.join(__dirname, 'demo-files');
  const root = process.env.TELEAGENT_FILES_ROOT || demo;
  if (path.resolve(root) === demo) return { root: demo };
  const secrets = {};
  let text = '';
  try { text = await fs.readFile(process.env.TELEAGENT_SECRET_ENV || '', 'utf8'); } catch { /* reported below */ }
  for (const line of text.split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line);
    if (match) secrets[match[1]] = match[2].trim().replace(/^(['"])(.*)\1$/, '$2');
  }
  const ownerId = process.env.TELEAGENT_OWNER_USER_ID || secrets.TELEAGENT_OWNER_USER_ID || secrets.TELEAGENT_CHAT_ID || '';
  if (!secrets.TELEAGENT_BOT_TOKEN || !/^\d+$/.test(ownerId)) {
    throw fail(500, 'Sharing a real folder needs TELEAGENT_SECRET_ENV to name this bot’s secret file, with its token and the owner’s private chat.');
  }
  return {
    root, readOnly: process.env.TELEAGENT_FILES_WRITABLE !== '1',
    telegram: { botToken: secrets.TELEAGENT_BOT_TOKEN, ownerId, maxAgeSeconds: 24 * 3600 },
  };
}

async function main() {
  const runtime = path.join(__dirname, '.runtime');
  await fs.mkdir(runtime, { recursive: true, mode: 0o700 });
  await fs.chmod(runtime, 0o700);
  const keyFile = path.join(runtime, 'access-key');
  let key;
  try { key = (await fs.readFile(keyFile, 'utf8')).trim(); }
  catch (error) { if (error.code !== 'ENOENT') throw error; key = crypto.randomBytes(32).toString('hex'); await fs.writeFile(keyFile, key, { flag: 'wx', mode: 0o600 }); }
  await fs.chmod(keyFile, 0o600);
  if (!/^[a-f0-9]{64}$/.test(key)) throw fail(500, 'The local access-key file must contain a 32-byte hexadecimal key.');
  const server = await createApp({ ...(await sharedFolder()), key, backupDir: path.join(runtime, 'backups') });
  const port = Number(process.env.DEMO_PORT || 8766);
  server.listen(port, '127.0.0.1', async () => {
    const url = `http://127.0.0.1:${port}/?key=${key}`;
    await fs.writeFile(path.join(runtime, 'local-url.txt'), url, { mode: 0o600 });
    await fs.chmod(path.join(runtime, 'local-url.txt'), 0o600);
    await fs.writeFile(path.join(__dirname, 'Open demo.html'), `<!doctype html><meta charset="utf-8"><title>Open Machine Files</title><meta http-equiv="refresh" content="0;url=${url}"><a href="${url}">Open Machine Files</a>`, { mode: 0o600 });
    await fs.chmod(path.join(__dirname, 'Open demo.html'), 0o600);
    console.log(`Machine Files is running on http://127.0.0.1:${port}. Open "Open demo.html" to connect.`);
  });
}
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { createApp, telegramUser };
