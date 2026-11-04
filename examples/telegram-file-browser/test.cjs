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
