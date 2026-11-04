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

async function createApp({ root, key, backupDir }) {
  root = await fs.realpath(root);
  let writes = Promise.resolve();
  async function resolveFile(relative = '') {
    if (typeof relative !== 'string' || relative.length > 500 || /[\\\x00-\x1f:]/.test(relative) || relative.startsWith('/')) throw fail(400, 'Invalid file path.');
    const segments = relative ? relative.split('/') : [];
    if (segments.some(part => !part || part === '.' || part === '..' || part.startsWith('.') || /[ .]$/.test(part))) throw fail(403, 'This path is outside the demo folder.');
    let target = root;
    for (const part of segments) {
      target = path.join(target, part);
      if ((await fs.lstat(target)).isSymbolicLink()) throw fail(403, 'Linked folders and files are not exposed.');
    }
    const real = await fs.realpath(target);
    const within = path.relative(root, real);
    if (within === '..' || within.startsWith('..' + path.sep) || path.isAbsolute(within)) throw fail(403, 'This path is outside the demo folder.');
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
      // Telegram's native downloader cannot attach the page's Authorization header.
      const downloadAuth = req.method === 'GET' && url.pathname === '/api/download' ? 'Bearer ' + url.searchParams.get('key') : '';
      const supplied = Buffer.from(req.headers.authorization || downloadAuth);
      const expected = Buffer.from('Bearer ' + key);
      if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) throw fail(401, 'Open the private demo link to connect to this folder.');
      const relative = url.searchParams.get('path') || '';
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
        return json(res, 200, { host: os.hostname(), root: path.basename(root), path: relative, entries });
      }
      if (req.method === 'GET' && url.pathname === '/api/file') {
        if (!stat.isFile()) throw fail(400, 'Select a file.');
        if (stat.size > MAX_FILE) throw fail(413, 'Preview is limited to files up to 10 MB.');
        const type = imageTypes[path.extname(target).toLowerCase()];
        if (type) return json(res, 200, { kind: 'image', type, data: (await fs.readFile(target)).toString('base64'), size: stat.size });
        if (!textExtensions.has(path.extname(target).toLowerCase())) return json(res, 200, { kind: 'download', size: stat.size });
        return json(res, 200, { kind: 'text', ...(await readText(target, stat)), size: stat.size });
      }
      if (req.method === 'GET' && url.pathname === '/api/download') {
        if (!stat.isFile()) throw fail(400, 'Select a file.');
        if (stat.size > MAX_FILE) throw fail(413, 'Downloads are limited to files up to 10 MB in this demo.');
        res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': "attachment; filename*=UTF-8''" + encodeURIComponent(path.basename(target)) });
        return res.end(await fs.readFile(target));
      }
      if (req.method === 'PUT' && url.pathname === '/api/file') {
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
  const server = await createApp({ root: path.join(__dirname, 'demo-files'), key, backupDir: path.join(runtime, 'backups') });
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
module.exports = { createApp };
