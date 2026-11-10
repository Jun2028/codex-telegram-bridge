'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const key = params.get('key') || sessionStorage.getItem('machine-files-key') || '';
if (key) sessionStorage.setItem('machine-files-key', key);
params.delete('key');
history.replaceState(null, '', location.pathname + (params.size ? '?' + params : '') + location.hash);
const tg = window.Telegram?.WebApp;
if (tg?.initData) {
  tg.ready(); tg.expand();
  function theme() {
    const p = tg.themeParams;
    for (const [variable, value] of Object.entries({ '--bg': p.secondary_bg_color, '--panel': p.bg_color, '--editor': p.bg_color, '--ink': p.text_color, '--muted': p.hint_color, '--accent': p.button_color })) if (value) document.documentElement.style.setProperty(variable, value);
  }
  theme(); tg.onEvent('themeChanged', theme);
}
let directory = '', listing = [], selected = '', current = null, original = '', busy = false, requestId = 0, readOnly = false, rootName = 'demo-files';
const bytes = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const dirty = () => !readOnly && current?.kind === 'text' && $('editor').value !== original;
function message(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(message.timer); message.timer = setTimeout(() => $('toast').hidden = true, 3600); }
async function api(route, file, options = {}) {
  const response = await fetch('/api/' + route + '?path=' + encodeURIComponent(file), { ...options, headers: { Authorization: 'Bearer ' + key, 'X-Telegram-Init-Data': tg?.initData || '', ...options.headers } });
  if (!response.ok) { const problem = await response.json(); throw new Error(problem.error || 'Request failed.'); }
  return route === 'download' ? response.blob() : response.json();
}
function canLeave() { return !dirty() || confirm('Discard the unsaved changes to ' + selected.split('/').pop() + '?'); }
function updateDirty() {
  const changed = dirty(); $('save').disabled = !changed || busy;
  $('save-state').textContent = readOnly ? 'Read-only' : changed ? 'Unsaved changes' : 'Saved on this machine';
  $('save-state').classList.toggle('dirty', changed);
  if (tg?.initData) { if (changed) tg.enableClosingConfirmation(); else tg.disableClosingConfirmation(); }
  $('line-numbers').textContent = Array.from({ length: $('editor').value.split('\n').length }, (_, i) => i + 1).join('\n');
}
function renderFiles() {
  const list = $('file-list'); list.replaceChildren();
  const visible = listing.filter(item => item.name.toLowerCase().includes($('filter').value.toLowerCase()));
  for (const item of visible) {
    const relative = [directory, item.name].filter(Boolean).join('/');
    const button = document.createElement('button'); button.className = 'file-row' + (relative === selected ? ' selected' : ''); button.setAttribute('role', 'listitem'); button.setAttribute('aria-label', (item.kind === 'folder' ? 'Open folder ' : 'Open file ') + item.name);
    const icon = document.createElement('span'); icon.className = 'file-icon' + (item.kind === 'folder' ? ' folder' : ''); icon.textContent = item.kind === 'folder' ? '▱' : item.name.includes('.') ? item.name.split('.').pop().slice(0,4).toUpperCase() : 'FILE'; icon.setAttribute('aria-hidden', 'true');
    const name = document.createElement('span'); name.className = 'name'; name.textContent = item.name;
    const size = document.createElement('span'); size.className = 'size'; size.textContent = item.kind === 'folder' ? '→' : bytes(item.size);
    button.append(icon, name, size); button.onclick = () => item.kind === 'folder' ? openDirectory(relative) : openFile(relative); list.append(button);
  }
  $('file-count').textContent = visible.length ? `${visible.length} ${visible.length === 1 ? 'item' : 'items'}` : 'No matching files';
}
async function openDirectory(relative, check = true) {
  if (busy || (check && !canLeave())) return;
  busy = true; $('editor').readOnly = true; updateDirty();
  const id = ++requestId;
  try {
    const data = await api('list', relative); if (id !== requestId) return;
    directory = data.path; listing = data.entries; readOnly = Boolean(data.readOnly); rootName = data.root; $('filter').value = ''; selected = ''; current = null; original = '';
    $('machine').textContent = data.host + ' · ' + data.root; $('connection').textContent = 'Connected'; $('connection').classList.add('connected');
    const nav = $('breadcrumbs'); nav.replaceChildren();
    const crumbs = [{ label: data.root, path: '' }]; let walk = '';
    for (const part of directory.split('/').filter(Boolean)) { walk = [walk, part].filter(Boolean).join('/'); crumbs.push({ label: part, path: walk }); }
    for (const [i, crumb] of crumbs.entries()) { if(i){const slash=document.createElement('span');slash.textContent='›';nav.append(slash);} const b=document.createElement('button');b.textContent=crumb.label;b.onclick=()=>openDirectory(crumb.path);nav.append(b); }
    $('file-view').hidden = true; $('empty').hidden = false; document.body.classList.remove('file-open'); updateDirty(); renderFiles();
  } catch(error) { $('connection').textContent = 'Disconnected'; $('connection').classList.remove('connected'); message(error.message); $('machine').textContent = error.message; }
  finally { busy = false; $('editor').readOnly = readOnly; updateDirty(); }
}
async function openFile(relative, check = true) {
  if (busy || (check && !canLeave())) return;
  busy = true; $('editor').readOnly = true; updateDirty();
  const id = ++requestId;
  try {
    const data = await api('file', relative); if (id !== requestId) return;
    selected = relative; current = data; $('notice').hidden = true; $('empty').hidden = true; $('file-view').hidden = false;
    $('file-name').textContent = relative.split('/').pop(); $('file-path').textContent = rootName + ' / ' + relative.split('/').join(' / ');
    $('text-view').hidden = data.kind !== 'text'; $('image-view').hidden = data.kind !== 'image'; $('binary-view').hidden = data.kind !== 'download'; $('save').hidden = readOnly || data.kind !== 'text'; $('reload').hidden = false;
    original = data.content || ''; $('editor').value = original; $('editor').scrollTop = 0;
    if (data.kind === 'image') { $('preview-image').src = `data:${data.type};base64,${data.data}`; $('preview-image').alt = relative.split('/').pop(); }
    $('file-details').textContent = bytes(data.size) + (data.kind === 'text' ? ' · UTF-8' : '');
    document.body.classList.add('file-open'); updateDirty(); renderFiles();
  } catch (error) { message(error.message); }
  finally { busy = false; $('editor').readOnly = readOnly; updateDirty(); }
}
async function save() {
  if (readOnly || !dirty() || busy) return;
  busy = true; $('editor').readOnly = true; $('save').textContent = 'Saving…'; updateDirty();
  const content = $('editor').value;
  try {
    const result = await api('file', selected, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content, revision: current.revision }) });
    current.revision = result.revision; original = content; $('notice').hidden = true;
    const item = listing.find(item => [directory, item.name].filter(Boolean).join('/') === selected); if (item) item.size = new TextEncoder().encode(content).length;
    $('file-details').textContent = bytes(new TextEncoder().encode(content).length) + ' · UTF-8'; renderFiles(); message('Saved to this machine.');
  } catch(error) { $('notice').textContent = error.message; $('notice').hidden = false; }
  finally { busy = false; $('editor').readOnly = readOnly; $('save').textContent = 'Save changes'; updateDirty(); }
}
$('filter').oninput = renderFiles;
$('editor').oninput = updateDirty;
$('editor').onscroll = () => $('line-numbers').scrollTop = $('editor').scrollTop;
$('save').onclick = save;
$('reload').onclick = () => openFile(selected);
$('refresh').onclick = () => openDirectory(directory);
$('start-demo').onclick = () => openFile('welcome.md');
$('back').onclick = () => { if(!busy && canLeave()){current=null;selected='';document.body.classList.remove('file-open');updateDirty();renderFiles();} };
$('download').onclick = async () => {
  try {
    if (tg?.initData && tg.isVersionAtLeast('8.0')) {
      const { ticket } = await api('ticket', selected);
      tg.downloadFile({ url: location.origin + '/api/download?path=' + encodeURIComponent(selected) + '&ticket=' + encodeURIComponent(ticket), file_name: selected.split('/').pop() });
      return;
    }
    const blob = await api('download', selected); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = selected.split('/').pop(); document.body.append(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),60000);
  } catch(error) { message(error.message); }
};
document.addEventListener('keydown', event => { if((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's'){event.preventDefault();save();} });
window.addEventListener('beforeunload', event => { if(dirty()){event.preventDefault();event.returnValue='';} });
openDirectory('', false);
