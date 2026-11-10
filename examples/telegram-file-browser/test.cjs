const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { createApp } = require('./server.cjs');

test('file service: real edits, conflict detection, authentication and folder boundary', async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'telegram-files-test-'));
  const root = path.join(temp, 'files'); await fs.mkdir(root);
  await fs.writeFile(path.join(root, 'note.md'), 'Original\n');
  await fs.writeFile(path.join(root, 'image.png'), Buffer.from([137,80,78,71]));
  await fs.writeFile(path.join(root, 'bad.txt'), Buffer.from([255,254]));
  await fs.writeFile(path.join(temp, 'outside.txt'), 'Private');
  await fs.mkdir(path.join(temp, 'outside'));
  await fs.symlink(path.join(temp, 'outside'), path.join(root, 'linked'), 'junction');
  const server = await createApp({ root, key: 'test-access-key', backupDir: path.join(temp, 'backups') });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = (route, file, options = {}) => fetch(base + '/api/' + route + '?path=' + encodeURIComponent(file), { ...options, headers: { Authorization: 'Bearer test-access-key', ...options.headers } });
  const put = (content, revision) => ({ method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content, revision }) });
  try {
    assert.equal((await fetch(base + '/api/list')).status, 401);
    for (const attempt of ['../outside.txt', 'C:/Windows/win.ini', '..\\outside.txt', 'note.md:stream', 'linked/file.txt', '.secret']) assert.ok([400,403].includes((await call('file', attempt)).status), attempt);
    assert.equal((await call('file', 'bad.txt')).status, 415);
    const listing = await (await call('list', '')).json(); assert.ok(listing.entries.some(e=>e.name==='note.md')); assert.ok(!listing.entries.some(e=>e.name==='linked'));
    const first = await (await call('file', 'note.md')).json(); assert.equal(first.content, 'Original\n');
    assert.equal((await call('file', 'note.md', put('Edited\n', first.revision))).status, 200);
    assert.equal(await fs.readFile(path.join(root, 'note.md'), 'utf8'), 'Edited\n');
    assert.equal((await call('file', 'note.md', put('Stale\n', first.revision))).status, 409);
    const latest = await (await call('file', 'note.md')).json();
    const race = await Promise.all([call('file', 'note.md', put('Writer A\n', latest.revision)), call('file', 'note.md', put('Writer B\n', latest.revision))]);
    assert.deepEqual(race.map(r=>r.status).sort(), [200,409]);
    assert.equal((await call('file', 'image.png', put('broken', latest.revision))).status, 415);
    assert.equal((await call('file', 'note.md', {method:'PUT',headers:{'Content-Type':'application/json'},body:'{'})).status,400);
    const download = await call('download', 'note.md'); assert.equal(await download.text(), await fs.readFile(path.join(root,'note.md'),'utf8'));
    assert.equal((await fetch(base + '/api/download?path=note.md&key=test-access-key')).status, 200);
    assert.equal((await fetch(base + '/api/download?path=note.md&key=wrong')).status, 401);
    assert.equal((await fetch(base + '/api/list?key=test-access-key')).status, 401);
    assert.ok((await fs.readdir(path.join(temp, 'backups'))).length >= 2);
    if (process.platform !== 'win32') {
      assert.equal((await fs.stat(path.join(temp, 'backups'))).mode & 0o777, 0o700);
      for (const file of await fs.readdir(path.join(temp, 'backups'))) assert.equal((await fs.stat(path.join(temp, 'backups', file))).mode & 0o777, 0o600);
    }
    assert.ok(!(await fs.readdir(root)).some(n=>n.startsWith('.save-')));
  } finally { await new Promise(resolve=>server.close(resolve)); await fs.rm(temp,{recursive:true,force:true}); }
});

test('startup protects existing credential files', { timeout: 10000 }, async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'telegram-files-startup-'));
  await fs.copyFile(path.join(__dirname, 'server.cjs'), path.join(temp, 'server.cjs'));
  await fs.mkdir(path.join(temp, 'demo-files'));
  await fs.mkdir(path.join(temp, '.runtime'), { mode: 0o755 });
  const credentials = ['.runtime/access-key', '.runtime/local-url.txt', 'Open demo.html'];
  for (const file of credentials) await fs.writeFile(path.join(temp, file), 'a'.repeat(64), { mode: 0o644 });
  const child = spawn(process.execPath, [path.join(temp, 'server.cjs')], { env: { ...process.env, DEMO_PORT: '0' }, stdio: ['ignore', 'pipe', 'pipe'] });
  try {
    await Promise.race([once(child.stdout, 'data'), once(child, 'exit').then(() => { throw new Error('File service exited before startup'); })]);
    if (process.platform !== 'win32') {
      assert.equal((await fs.stat(path.join(temp, '.runtime'))).mode & 0o777, 0o700);
      for (const file of credentials) assert.equal((await fs.stat(path.join(temp, file))).mode & 0o777, 0o600);
    }
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = once(child, 'exit'); child.kill(); await exited;
    }
    await fs.rm(temp, { recursive: true, force: true });
  }
});

test('real folder: owner-only through Telegram, read-only, hidden files stay hidden', async () => {
  const crypto = require('node:crypto');
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'telegram-files-real-'));
  const root = path.join(temp, 'home'); await fs.mkdir(path.join(root, '.ssh'), { recursive: true });
  await fs.writeFile(path.join(root, 'notes.md'), 'Mine\n');
  await fs.writeFile(path.join(root, '.ssh', 'id_key'), 'private');
  await fs.writeFile(path.join(root, '.env'), 'SECRET=1');
  const botToken = '123456:test-token', ownerId = '4242';
  // What Telegram would hand the page, signed with the bot's token.
  const initData = (userId, ageSeconds = 0, token = botToken) => {
    const fields = { auth_date: String(Math.floor(Date.now() / 1000) - ageSeconds), query_id: 'q1', user: JSON.stringify({ id: userId, first_name: 'T' }) };
    const check = Object.keys(fields).sort().map(name => name + '=' + fields[name]).join('\n');
    const secret = crypto.createHmac('sha256', 'WebAppData').update(token).digest();
    return new URLSearchParams({ ...fields, hash: crypto.createHmac('sha256', secret).update(check).digest('hex') }).toString();
  };
  const server = await createApp({ root, key: 'test-access-key', backupDir: path.join(temp, 'backups'), readOnly: true,
    telegram: { botToken, ownerId, maxAgeSeconds: 3600 } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = (route, file, { init = initData(4242), key = 'test-access-key', ...options } = {}) =>
    fetch(base + '/api/' + route + '?path=' + encodeURIComponent(file), { ...options,
      headers: { Authorization: 'Bearer ' + key, ...(init === null ? {} : { 'X-Telegram-Init-Data': init }), ...options.headers } });
  try {
    // The private link alone is no longer enough: Telegram must vouch for the owner.
    assert.equal((await call('list', '', { init: null })).status, 401);
    assert.equal((await call('list', '', { init: initData(9999) })).status, 401, 'another Telegram user');
    assert.equal((await call('list', '', { init: initData(4242, 7200) })).status, 401, 'stale sign-in');
    assert.equal((await call('list', '', { init: initData(4242, 0, 'another-bot-token') })).status, 401, 'forged signature');
    assert.equal((await call('list', '', { init: initData(4242).replace('q1', 'q2') })).status, 401, 'altered data');
    assert.equal((await call('list', '', { key: 'wrong' })).status, 401, 'right person, wrong link');
    const listing = await (await call('list', '')).json();
    assert.deepEqual(listing.entries.map(entry => entry.name), ['notes.md']);
    assert.equal(listing.readOnly, true);
    assert.equal((await (await call('file', 'notes.md')).json()).content, 'Mine\n');
    for (const hidden of ['.env', '.ssh/id_key', '.ssh']) assert.equal((await call('file', hidden)).status, 403, hidden);
    // Read-only: a save is refused and the file is untouched.
    const first = await (await call('file', 'notes.md')).json();
    const save = await call('file', 'notes.md', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: 'Changed\n', revision: first.revision }) });
    assert.equal(save.status, 403);
    assert.equal(await fs.readFile(path.join(root, 'notes.md'), 'utf8'), 'Mine\n');
    // Downloads: the key in the address no longer works; a ticket does, for that file only, briefly.
    assert.equal((await fetch(base + '/api/download?path=notes.md&key=test-access-key')).status, 401);
    assert.equal((await call('ticket', 'notes.md', { init: initData(9999) })).status, 401);
    const { ticket } = await (await call('ticket', 'notes.md')).json();
    const download = await fetch(base + '/api/download?path=notes.md&ticket=' + encodeURIComponent(ticket));
    assert.equal(download.status, 200); assert.equal(await download.text(), 'Mine\n');
    assert.equal((await fetch(base + '/api/download?path=.env&ticket=' + encodeURIComponent(ticket))).status, 401, 'ticket for another file');
    assert.equal((await fetch(base + '/api/list?ticket=' + encodeURIComponent(ticket))).status, 401, 'ticket is for downloads only');
    assert.equal((await fetch(base + '/api/download?path=notes.md&ticket=' + encodeURIComponent('1.' + ticket.split('.')[1]))).status, 401, 'expired ticket');
  } finally { await new Promise(resolve => server.close(resolve)); await fs.rm(temp, { recursive: true, force: true }); }
});

test('a real folder refuses to start without the bot secret', { timeout: 10000 }, async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'telegram-files-refuse-'));
  await fs.mkdir(path.join(temp, 'storage'));
  const child = spawn(process.execPath, [path.join(__dirname, 'server.cjs')], {
    env: { ...process.env, DEMO_PORT: '0', TELEAGENT_FILES_ROOT: path.join(temp, 'storage'), TELEAGENT_SECRET_ENV: path.join(temp, 'absent.env') },
    stdio: ['ignore', 'pipe', 'pipe'] });
  let errors = ''; child.stderr.on('data', chunk => { errors += chunk; });
  const [code] = await once(child, 'exit');
  await fs.rm(temp, { recursive: true, force: true });
  assert.notEqual(code, 0);
  assert.match(errors, /Sharing a real folder needs TELEAGENT_SECRET_ENV/);
});
