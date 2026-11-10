'use strict';
const $ = id => document.getElementById(id);

// The private key arrives once in the address and is then removed from it.
const params = new URLSearchParams(location.search);
const key = params.get('key') || sessionStorage.getItem('machine-files-key') || '';
if (key) sessionStorage.setItem('machine-files-key', key);
params.delete('key');
history.replaceState(null, '', location.pathname + (params.size ? '?' + params : '') + location.hash);

// Inside Telegram: follow its colours and tell the page who opened it.
const tg = window.Telegram?.WebApp;
if (tg?.initData) {
  tg.ready();
  tg.expand();
  const applyTheme = () => {
    const theme = tg.themeParams;
    const colours = { '--bg': theme.secondary_bg_color, '--surface': theme.bg_color, '--ink': theme.text_color,
      '--muted': theme.hint_color, '--accent': theme.button_color, '--on-accent': theme.button_text_color };
    for (const [variable, value] of Object.entries(colours)) if (value) document.documentElement.style.setProperty(variable, value);
    document.documentElement.dataset.scheme = tg.colorScheme;
  };
  applyTheme();
  tg.onEvent('themeChanged', applyTheme);
}

const ICONS = {
  folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2h7A1.5 1.5 0 0 1 19 8.5v8A1.5 1.5 0 0 1 17.5 18h-13A1.5 1.5 0 0 1 3 16.5z"/>',
  code: '<path d="M8 8l-4 4 4 4M14 8l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  text: '<path d="M6 3.5h7l4 4v11a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 11h6M8 14h6M8 8h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
  image: '<rect x="3.5" y="5" width="15" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="9.5" r="1.4"/><path d="M4 16l4.5-4 3 2.5 3-3 4 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>',
  data: '<rect x="3.5" y="4.5" width="15" height="13" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M3.5 9h15M3.5 13h15M9 4.5v13" stroke="currentColor" stroke-width="1.5"/>',
  file: '<path d="M6 3.5h7l4 4v11a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M13 3.5v4h4" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  refresh: '<path d="M17 11a6 6 0 1 1-1.8-4.3M17 4v3.5h-3.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  back: '<path d="M12.5 5l-6 6 6 6M7 11h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  download: '<path d="M11 4v9M7 10l4 4 4-4M5 17h12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  search: '<circle cx="10" cy="10" r="5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M14 14l4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
};
const KINDS = {
  code: 'js cjs mjs ts tsx jsx py sh bash css html htm xml svg json yaml yml toml',
  text: 'md txt log',
  data: 'csv tsv',
  image: 'png jpg jpeg webp gif',
};
// These drawings are the fixed strings above, never taken from a file or the network.
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 22 22');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[name];
  return svg;
}
function kindOf(item) {
  if (item.kind === 'folder') return 'folder';
  const extension = item.name.includes('.') ? item.name.split('.').pop().toLowerCase() : '';
  return Object.keys(KINDS).find(kind => KINDS[kind].split(' ').includes(extension)) || 'file';
}
for (const id of ['refresh', 'back', 'download']) $(id).append(icon(id));
document.querySelector('.search-icon').append(icon('search'));

const state = { directory: '', listing: [], selected: '', file: null, original: '', busy: false, request: 0,
  readOnly: false, root: '', editing: false };
const baseName = file => file.split('/').pop();
const size = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
function when(iso) {
  const date = new Date(iso), days = (Date.now() - date) / 86400000;
  if (days < 1) return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return date.toLocaleDateString([], days < 300 ? { day: 'numeric', month: 'short' } : { year: 'numeric', month: 'short' });
}
const dirty = () => state.editing && $('editor').value !== state.original;

function toast(text) {
  $('toast').textContent = text;
  $('toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $('toast').hidden = true; }, 3600);
}

async function api(route, file, options = {}) {
  const response = await fetch('/api/' + route + '?path=' + encodeURIComponent(file), { ...options,
    headers: { Authorization: 'Bearer ' + key, 'X-Telegram-Init-Data': tg?.initData || '', ...options.headers } });
  if (!response.ok) throw new Error((await response.json()).error || 'Request failed.');
  return route === 'download' ? response.blob() : response.json();
}

const canLeave = () => !dirty() || confirm('Discard the unsaved changes to ' + baseName(state.selected) + '?');

function showStatus() {
  const changed = dirty();
  $('save').disabled = !changed || state.busy;
  $('save-state').textContent = state.readOnly ? 'Read-only' : changed ? 'Unsaved changes' : state.editing ? 'No changes' : '';
  $('save-state').classList.toggle('dirty', changed);
  if (tg?.initData) { if (changed) tg.enableClosingConfirmation(); else tg.disableClosingConfirmation(); }
}

function renderList() {
  const list = $('file-list');
  list.replaceChildren();
  const needle = $('filter').value.toLowerCase();
  const visible = state.listing.filter(item => item.name.toLowerCase().includes(needle));
  for (const item of visible) {
    const relative = [state.directory, item.name].filter(Boolean).join('/');
    const kind = kindOf(item);
    const row = document.createElement('button');
    row.className = 'row' + (relative === state.selected ? ' selected' : '');
    row.setAttribute('role', 'listitem');
    const badge = document.createElement('span');
    badge.className = 'badge ' + kind;
    badge.append(icon(kind));
    const text = document.createElement('span');
    text.className = 'row-text';
    const name = document.createElement('span');
    name.className = 'row-name';
    name.textContent = item.name;
    const detail = document.createElement('span');
    detail.className = 'row-detail';
    detail.textContent = (item.kind === 'folder' ? 'Folder' : size(item.size)) + ' · ' + when(item.modified);
    text.append(name, detail);
    row.append(badge, text);
    if (item.kind === 'folder') {
      const chevron = document.createElement('span');
      chevron.className = 'chevron';
      chevron.textContent = '›';
      row.append(chevron);
    }
    row.onclick = () => item.kind === 'folder' ? openDirectory(relative) : openFile(relative);
    list.append(row);
  }
  $('file-count').textContent = !state.listing.length ? 'This folder is empty.'
    : !visible.length ? 'Nothing matches the filter.'
    : visible.length + (visible.length === 1 ? ' item' : ' items');
}

function renderBreadcrumbs() {
  const nav = $('breadcrumbs');
  nav.replaceChildren();
  const crumbs = [{ label: state.root, path: '' }];
  let walk = '';
  for (const part of state.directory.split('/').filter(Boolean)) {
    walk = [walk, part].filter(Boolean).join('/');
    crumbs.push({ label: part, path: walk });
  }
  crumbs.forEach((crumb, index) => {
    if (index) {
      const separator = document.createElement('span');
      separator.textContent = '/';
      nav.append(separator);
    }
    const button = document.createElement('button');
    button.textContent = crumb.label;
    if (index === crumbs.length - 1) button.setAttribute('aria-current', 'page');
    button.onclick = () => openDirectory(crumb.path);
    nav.append(button);
  });
  nav.scrollLeft = nav.scrollWidth;
}

function closeFile() {
  Object.assign(state, { selected: '', file: null, original: '', editing: false });
  $('file-view').hidden = true;
  $('empty').hidden = false;
  document.body.classList.remove('file-open');
}

async function openDirectory(relative, check = true) {
  if (state.busy || (check && !canLeave())) return;
  state.busy = true;
  const id = ++state.request;
  try {
    const data = await api('list', relative);
    if (id !== state.request) return;
    Object.assign(state, { directory: data.path, listing: data.entries, readOnly: Boolean(data.readOnly), root: data.root });
    $('filter').value = '';
    $('machine').textContent = data.host + (state.readOnly ? ' · read-only' : '');
    closeFile();
    renderBreadcrumbs();
    renderList();
    showStatus();
    $('file-list').scrollTop = 0;
  } catch (error) {
    $('machine').textContent = 'Not connected';
    $('file-count').textContent = error.message;
    toast(error.message);
  } finally { state.busy = false; }
}

function showText(content) {
  const name = baseName(state.selected);
  window.Highlight.render($('code'), content, name);
  $('line-numbers').textContent = Array.from({ length: content.split('\n').length }, (_, index) => index + 1).join('\n');
  // Prose wraps and needs no line numbers; code keeps its lines intact.
  $('code-view').classList.toggle('prose', window.Highlight.isProse(name));
  $('code-view').scrollTop = 0;
  $('code-view').scrollLeft = 0;
}

function setEditing(on) {
  state.editing = on;
  const isText = state.file?.kind === 'text';
  $('code-view').hidden = on || !isText;
  $('editor').hidden = !on;
  $('edit').hidden = on || state.readOnly || !isText;
  $('save').hidden = !on;
  if (on) { $('editor').value = state.original; $('editor').focus(); }
  showStatus();
}

async function openFile(relative, check = true) {
  if (state.busy || (check && !canLeave())) return;
  state.busy = true;
  const id = ++state.request;
  try {
    const data = await api('file', relative);
    if (id !== state.request) return;
    Object.assign(state, { selected: relative, file: data, original: data.content || '' });
    $('notice').hidden = true;
    $('empty').hidden = true;
    $('file-view').hidden = false;
    $('file-name').textContent = baseName(relative);
    $('file-path').textContent = [state.root, ...relative.split('/').slice(0, -1)].join(' / ');
    $('image-view').hidden = data.kind !== 'image';
    $('binary-view').hidden = data.kind !== 'download';
    if (data.kind === 'text') showText(state.original);
    if (data.kind === 'image') {
      $('preview-image').src = `data:${data.type};base64,${data.data}`;
      $('preview-image').alt = baseName(relative);
    }
    $('file-details').textContent = size(data.size)
      + (data.kind === 'text' ? ' · ' + state.original.split('\n').length + ' lines' : '');
    setEditing(false);
    document.body.classList.add('file-open');
    renderList();
  } catch (error) { toast(error.message); }
  finally { state.busy = false; showStatus(); }
}

async function save() {
  if (state.readOnly || !dirty() || state.busy) return;
  state.busy = true;
  $('save').textContent = 'Saving…';
  showStatus();
  const content = $('editor').value;
  try {
    const result = await api('file', state.selected, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, revision: state.file.revision }) });
    state.file.revision = result.revision;
    state.original = content;
    $('notice').hidden = true;
    const bytes = new TextEncoder().encode(content).length;
    const item = state.listing.find(entry => [state.directory, entry.name].filter(Boolean).join('/') === state.selected);
    if (item) { item.size = bytes; item.modified = result.savedAt; }
    $('file-details').textContent = size(bytes) + ' · ' + content.split('\n').length + ' lines';
    showText(content);
    setEditing(false);
    renderList();
    toast('Saved to this machine.');
  } catch (error) {
    $('notice').textContent = error.message;
    $('notice').hidden = false;
  } finally { state.busy = false; $('save').textContent = 'Save'; showStatus(); }
}

async function download() {
  try {
    if (tg?.initData && tg.isVersionAtLeast('8.0')) {
      const { ticket } = await api('ticket', state.selected);
      tg.downloadFile({ url: location.origin + '/api/download?path=' + encodeURIComponent(state.selected)
        + '&ticket=' + encodeURIComponent(ticket), file_name: baseName(state.selected) });
      return;
    }
    const url = URL.createObjectURL(await api('download', state.selected));
    const link = document.createElement('a');
    link.href = url;
    link.download = baseName(state.selected);
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) { toast(error.message); }
}

$('filter').oninput = renderList;
$('editor').oninput = showStatus;
$('edit').onclick = () => setEditing(true);
$('save').onclick = save;
$('download').onclick = download;
$('reload').onclick = () => openFile(state.selected);
$('refresh').onclick = () => openDirectory(state.directory);
$('back').onclick = () => { if (!state.busy && canLeave()) { closeFile(); showStatus(); renderList(); } };
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); }
});
window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
openDirectory('', false);
