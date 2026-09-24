import { buildIndex, shortestPath, relationLabel, NODE_TYPES, FAMILIES, familyOf } from './model.js';
import { createGraphView } from './graph-view.js';
import { createTimelineView } from './timeline-view.js';

const $ = (s) => document.querySelector(s);
const statusEl = $('#status');

// ---- theme ----------------------------------------------------------------------
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};
const savedTheme = store.get('mythographia-theme');
if (savedTheme) document.documentElement.dataset.theme = savedTheme;

// ---- load -----------------------------------------------------------------------
let data;
try {
  const res = await fetch('/api/graph');
  if (!res.ok) throw new Error(`The archive answered ${res.status}.`);
  data = await res.json();
} catch (err) {
  statusEl.textContent = `The archive could not be reached. ${err.message}`;
  throw err;
}
if (!data.nodes?.length) {
  statusEl.textContent = 'The archive is empty — run the Wikipedia ingest first.';
  throw new Error('empty graph');
}
const index = buildIndex(data);
const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fold = (s) => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const subtype = (n) => NODE_TYPES[n.type]?.label ?? n.type;
const swatch = (n) => `<span class="swatch ${n.type === 'event' ? 'event' : ''}" style="--c: var(--${familyOf(n)})"></span>`;
const genLabel = (n) => (n.generation == null ? '' : ` · gen ${n.generation.toFixed(1)}`);

$('#data-stamp').textContent = `${index.nodes.length} entries · ${index.edges.length} ties${
  data.meta?.ingestedAt ? ` · fetched ${new Date(data.meta.ingestedAt).toISOString().slice(0, 10)}` : ''}`;

// ---- state ----------------------------------------------------------------------
let view = 'graph';
let selected = null;

// ---- views ----------------------------------------------------------------------
const hover = $('#hovercard');
function showHover(id, x, y, host) {
  if (!id) { hover.hidden = true; return; }
  const n = index.byId.get(id);
  const img = n.image?.thumb ? n.image.thumb.replace(/\/\d+px-/, '/330px-') : null;
  hover.innerHTML = `
    ${img ? `<div class="poster" style="background-image:url('${encodeURI(img)}')"></div>` : ''}
    <div class="inner">
      <div class="kicker">${swatch(n)} ${escapeHtml(subtype(n))}${escapeHtml(genLabel(n))}</div>
      <h3>${escapeHtml(n.title)}</h3>
      ${n.description ? `<p>${escapeHtml(n.description)}</p>` : ''}
      <p class="meta">${index.degree.get(id)} ties${n.image?.artist ? ` · image: ${escapeHtml(n.image.artist)}` : ''}</p>
    </div>`;
  hover.hidden = false;
  const stage = $('#stage').getBoundingClientRect();
  const hostRect = host.getBoundingClientRect();
  const w = 240;
  const h = hover.offsetHeight;
  let left = hostRect.left - stage.left + x + 18;
  let top = hostRect.top - stage.top + y - 20;
  if (left + w > stage.width - 12) left = left - w - 36;
  if (top + h > stage.height - 12) top = stage.height - h - 12;
  hover.style.left = `${Math.max(8, left)}px`;
  hover.style.top = `${Math.max(8, top)}px`;
}

const graph = createGraphView({
  canvas: $('#graph-canvas'),
  index,
  onHover: (id, x, y) => showHover(id, x, y, $('#graph-canvas')),
  onSelect: (id) => select(id),
  onExpand: (id) => {
    graph.expand(id);
    select(id, { fly: false });
  },
});
graph.showCore();
statusEl.classList.add('done');
setTimeout(() => graph.fitAll(1200), 900);

let timeline = null;
function ensureTimeline() {
  if (timeline) return timeline;
  timeline = createTimelineView({
    svgEl: $('#timeline-svg'), scrubEl: $('#scrub'), labelEl: $('#scrub-label'), playEl: $('#play'), index,
    onHover: (id, x, y) => showHover(id, x, y, $('#timeline-svg')),
    onSelect: (id) => select(id),
  });
  return timeline;
}

function setView(next, { push = true } = {}) {
  view = next;
  for (const b of document.querySelectorAll('.views button')) b.setAttribute('aria-selected', String(b.dataset.view === next));
  $('#graph-view').hidden = next === 'timeline';
  $('#timeline-view').hidden = next !== 'timeline';
  $('#path-panel').hidden = next !== 'path';
  $('#legend').hidden = next !== 'graph';
  graph.setInsetLeft(next === 'path' && window.innerWidth > 760 ? $('#path-panel').offsetWidth + 24 : 0);
  document.body.dataset.view = next;
  hover.hidden = true;
  if (next === 'timeline') {
    const t = ensureTimeline();
    requestAnimationFrame(() => { t.layout(); t.select(selected); });
  } else {
    graph.setPath(null);
    if (next === 'path') $('#path-from').focus();
  }
  if (push) writeHash();
}
for (const b of document.querySelectorAll('.views button')) b.addEventListener('click', () => setView(b.dataset.view));

// ---- detail panel ----------------------------------------------------------------
const detail = $('#detail');
const REL_GROUPS = [
  ['Parents', (s) => s.rel === 'parent' && s.dir === -1],
  ['Children', (s) => s.rel === 'parent' && s.dir === 1],
  ['Consorts', (s) => s.rel === 'consort'],
  ['Siblings', (s) => s.rel === 'sibling'],
  ['Appears in', (s) => s.rel === 'participant' && s.dir === 1],
  ['Featuring', (s) => s.rel === 'participant' && s.dir === -1],
  ['Linked with', (s) => s.rel === 'associated'],
];

function renderDetail(id) {
  if (!id) { detail.hidden = true; graph.setInsetRight(0); graph.setInsetBottom(0); return; }
  const n = index.byId.get(id);
  const paras = (n.extract || '').split(/\n+/).filter(Boolean);
  const img = n.image;
  const steps = [...index.adj.get(id)].sort((a, b) => index.byId.get(a.id).title.localeCompare(index.byId.get(b.id).title));
  const groups = REL_GROUPS.map(([label, test]) => {
    const seen = new Set();
    const items = steps.filter(test).filter((s) => !seen.has(s.id) && seen.add(s.id));
    if (!items.length) return '';
    return `<h4>${label}</h4><ul>${items.map((s) => {
      const m = index.byId.get(s.id);
      return `<li><button data-go="${escapeHtml(m.id)}">${swatch(m)}${escapeHtml(m.title)}</button></li>`;
    }).join('')}</ul>`;
  }).join('');
  const revUrl = n.revision ? `https://en.wikipedia.org/w/index.php?oldid=${n.revision}` : n.url;
  detail.innerHTML = `
    <button class="icon-btn close" aria-label="Close">×</button>
    ${img ? `<figure>
      <img src="${escapeHtml(img.thumb)}" alt="${escapeHtml(img.title || n.title)}" referrerpolicy="no-referrer" />
      <figcaption>${[img.title, img.artist, img.date].filter(Boolean).map(escapeHtml).join(' · ')}
        · <a href="${escapeHtml(img.page)}" target="_blank" rel="noopener">${escapeHtml(img.license || 'Public domain')}, Wikimedia Commons</a></figcaption>
    </figure>` : ''}
    <div class="body">
      <div class="kicker">${swatch(n)} ${escapeHtml(subtype(n))}${escapeHtml(genLabel(n))}</div>
      <h2>${escapeHtml(n.title)}</h2>
      ${n.description ? `<p class="desc">${escapeHtml(n.description)}</p>` : ''}
      <div class="extract">${paras.slice(0, 1).map((p) => `<p>${escapeHtml(p)}</p>`).join('')}
        <div class="rest" hidden>${paras.slice(1).map((p) => `<p>${escapeHtml(p)}</p>`).join('')}</div>
        ${paras.length > 1 ? '<button class="more">Continue reading</button>' : ''}</div>
      ${n.funFact ? `<div class="fact"><h4>From “${escapeHtml(n.funFact.section)}”</h4><p>${escapeHtml(n.funFact.text)}</p></div>` : ''}
      <div class="rels">${groups}</div>
      <div class="actions">
        ${view !== 'timeline' ? '<button class="chip" data-act="expand">Expand in web</button>' : '<button class="chip" data-act="web">Show in web</button>'}
        ${view !== 'timeline' ? '<button class="chip" data-act="timeline">See on timeline</button>' : ''}
        <button class="chip" data-act="from">Trace a thread from here</button>
      </div>
      <p class="src">Text: “<a href="${escapeHtml(n.url)}" target="_blank" rel="noopener">${escapeHtml(n.title)}</a>”, English Wikipedia${n.revision ? ` (<a href="${escapeHtml(revUrl)}" target="_blank" rel="noopener">revision ${n.revision}</a>)` : ''},
        CC BY-SA 4.0, excerpted without changes.</p>
    </div>`;
  detail.hidden = false;
  detail.scrollTop = 0;
  const wide = window.innerWidth > 760;
  graph.setInsetRight(wide ? detail.offsetWidth + 16 : 0);
  graph.setInsetBottom(wide ? 0 : detail.offsetHeight + 8);
}

detail.addEventListener('click', (ev) => {
  const t = ev.target.closest('button');
  if (!t) return;
  if (t.classList.contains('close')) return select(null);
  if (t.classList.contains('more')) {
    detail.querySelector('.rest').hidden = false;
    t.remove();
    return;
  }
  if (t.dataset.go) return goTo(t.dataset.go);
  const act = t.dataset.act;
  if (act === 'expand') { graph.expand(selected); graph.flyTo(selected, { k: 1.3 }); }
  if (act === 'timeline') setView('timeline');
  if (act === 'web') { setView('graph'); goTo(selected); }
  if (act === 'from') {
    pathEnds.from = selected;
    $('#path-from').value = index.byId.get(selected).title;
    setView('path');
    $('#path-to').focus();
  }
});

function select(id, { fly = true } = {}) {
  selected = id;
  graph.setFocus(id);
  renderDetail(id);
  timeline?.select(id);
  if (id && fly && view !== 'timeline') {
    if (!graph.onStage(id)) graph.expand(id);
    setTimeout(() => graph.flyTo(id), graph.onStage(id) ? 0 : 450);
  }
  writeHash();
}

function goTo(id) {
  if (view === 'path') setView('graph');
  if (view === 'graph' && !graph.onStage(id)) graph.expand(id);
  select(id);
}

// ---- legend / filters ------------------------------------------------------------
const hidden = new Set();
function renderLegend() {
  const counts = new Map();
  for (const n of index.nodes) counts.set(familyOf(n), (counts.get(familyOf(n)) || 0) + 1);
  $('#legend').innerHTML = `<h3>Filter</h3><div class="fam">${Object.entries(FAMILIES).map(([key, f]) => `
    <button data-fam="${key}" aria-pressed="${!hidden.has(key)}" title="${Object.values(NODE_TYPES).filter((t) => t.family === key).map((t) => t.label).join(', ')}">
      <span class="swatch ${key === 'event' ? 'event' : ''}" style="--c: var(--${key})"></span>${f.label} <span style="color:var(--muted)">${counts.get(key) || 0}</span>
    </button>`).join('')}</div>
    <div class="edge-key"><span><i></i>parentage</span><span><i class="dash"></i>appears in</span><span><i class="faint"></i>linked</span></div>`;
}
renderLegend();
$('#legend').addEventListener('click', (ev) => {
  const b = ev.target.closest('button[data-fam]');
  if (!b) return;
  const f = b.dataset.fam;
  hidden.has(f) ? hidden.delete(f) : hidden.add(f);
  if (hidden.size === Object.keys(FAMILIES).length) hidden.delete(f);
  graph.setHiddenFamilies(hidden);
  renderLegend();
});

$('#show-all').addEventListener('click', (ev) => {
  const all = !graph.isShowingAll();
  all ? graph.showAll() : graph.showCore();
  ev.target.textContent = all ? 'Fewer' : 'Show all';
  ev.target.setAttribute('aria-pressed', String(all));
  setTimeout(() => graph.fitAll(1100), 700);
});
$('#reset-view').addEventListener('click', () => { select(null); graph.fitAll(900); });

// ---- search --------------------------------------------------------------------
const titles = index.nodes.map((n) => ({ n, key: fold(n.title), desc: fold(n.description || '') }));
function search(q, { people = false } = {}) {
  const f = fold(q.trim());
  if (!f) return [];
  const out = [];
  for (const t of titles) {
    if (people && t.n.type === 'event') continue;
    let s = 0;
    if (t.key.startsWith(f)) s = 4;
    else if (t.key.split(/[\s(]+/).some((w) => w.startsWith(f))) s = 3;
    else if (t.key.includes(f)) s = 2;
    else if (f.length > 3 && t.desc.includes(f)) s = 1;
    if (s) out.push({ n: t.n, s: s * 1000 + (index.degree.get(t.n.id) || 0) });
  }
  return out.sort((a, b) => b.s - a.s).slice(0, 8).map((x) => x.n);
}

function attachSearch(input, list, onPick, opts) {
  let items = [];
  let active = 0;
  const render = () => {
    list.innerHTML = items.map((n, i) => `<li role="option" data-id="${escapeHtml(n.id)}" aria-selected="${i === active}">
      ${swatch(n)}<span class="name">${highlight(n.title, input.value)}</span><span class="sub">${escapeHtml(subtype(n))}</span></li>`).join('');
    list.hidden = !items.length;
    list.dataset.owner = input.id;
  };
  input.addEventListener('input', () => { items = search(input.value, opts); active = 0; render(); });
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') { active = Math.min(items.length - 1, active + 1); render(); ev.preventDefault(); }
    else if (ev.key === 'ArrowUp') { active = Math.max(0, active - 1); render(); ev.preventDefault(); }
    else if (ev.key === 'Enter' && items[active]) { pick(items[active]); ev.preventDefault(); }
    else if (ev.key === 'Escape') { list.hidden = true; input.blur(); }
  });
  input.addEventListener('blur', () => setTimeout(() => {
    if (list.dataset.owner === input.id && document.activeElement !== input) list.hidden = true;
  }, 150));
  list.addEventListener('pointerdown', (ev) => {
    const li = ev.target.closest('li');
    if (li && list.dataset.owner === input.id) { ev.preventDefault(); pick(index.byId.get(li.dataset.id)); }
  });
  function pick(n) {
    list.hidden = true;
    items = [];
    onPick(n, input);
  }
}
function highlight(title, q) {
  const f = fold(q.trim());
  const i = fold(title).indexOf(f);
  if (!f || i < 0) return escapeHtml(title);
  return `${escapeHtml(title.slice(0, i))}<mark>${escapeHtml(title.slice(i, i + f.length))}</mark>${escapeHtml(title.slice(i + f.length))}`;
}

attachSearch($('#search'), $('#search-results'), (n, input) => {
  input.value = '';
  input.blur();
  if (view === 'path') setView('graph');
  goTo(n.id);
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === '/' && document.activeElement.tagName !== 'INPUT') { ev.preventDefault(); $('#search').focus(); }
  if (ev.key === 'Escape' && document.activeElement.tagName !== 'INPUT') select(null);
});

// ---- six degrees ---------------------------------------------------------------
const pathEnds = { from: null, to: null };
let pathTimers = [];
for (const key of ['from', 'to']) {
  const input = $(`#path-${key}`);
  attachSearch(input, $('#path-suggest'), (n) => {
    pathEnds[key] = n.id;
    input.value = n.title;
    if (key === 'from' && !pathEnds.to) $('#path-to').focus();
    else if (pathEnds.from && pathEnds.to) runPath();
  }, { people: true });
  input.addEventListener('input', () => (pathEnds[key] = null));
  input.addEventListener('focus', () => {
    const list = $('#path-suggest');
    list.style.top = `${input.offsetTop + input.offsetHeight + 4}px`;
  });
}
$('#path-go').addEventListener('click', runPath);
$('#path-random').addEventListener('click', () => {
  const pool = index.nodes.filter((n) => n.type !== 'event' && index.degree.get(n.id) >= 2);
  for (let tries = 0; tries < 50; tries++) {
    const a = pool[Math.floor(Math.random() * pool.length)];
    const b = pool[Math.floor(Math.random() * pool.length)];
    const p = a !== b && shortestPath(index, a.id, b.id, pathOpts());
    if (p && p.length >= 4) {
      pathEnds.from = a.id;
      pathEnds.to = b.id;
      $('#path-from').value = a.title;
      $('#path-to').value = b.title;
      return runPath();
    }
  }
});
const pathOpts = () => ($('#family-only').checked ? { exclude: new Set(['participant', 'associated']) } : {});

function runPath() {
  const out = $('#path-result');
  pathTimers.forEach(clearTimeout);
  pathTimers = [];
  if (!pathEnds.from || !pathEnds.to) {
    out.innerHTML = '<li class="on note">Choose both figures from the suggestions.</li>';
    return;
  }
  const p = shortestPath(index, pathEnds.from, pathEnds.to, pathOpts());
  if (!p) {
    out.innerHTML = `<li class="on note">No thread joins them${$('#family-only').checked ? ' through blood or marriage alone' : ''}.</li>`;
    graph.setPath(null);
    writeHash();
    return;
  }
  const ids = p.map((s) => s.id);
  select(null);
  if (view !== 'path') setView('path', { push: false });
  const added = graph.reveal(ids, ids[0]);
  const deg = ids.length - 1;
  out.innerHTML = p.map((s, i) => `
    ${i ? `<li class="rel" data-i="${i}">${escapeHtml(relationLabel(s.via))}</li>` : ''}
    <li data-i="${i}"><button class="node" data-go="${escapeHtml(s.id)}">${swatch(index.byId.get(s.id))} ${escapeHtml(index.byId.get(s.id).title)}</button></li>`).join('')
    + `<li class="note" data-i="${deg}">${deg} degree${deg === 1 ? '' : 's'} of separation.</li>`;
  const start = () => {
    graph.fitTo(ids, { duration: 900 });
    pathTimers.push(setTimeout(() => {
      graph.setPath(ids);
      const hop = graph.pathDuration([0, 1]);
      for (const li of out.querySelectorAll('li')) {
        const i = Number(li.dataset.i);
        pathTimers.push(setTimeout(() => li.classList.add('on'), Math.max(0, i - (li.classList.contains('rel') ? 0.5 : 0)) * hop));
      }
    }, 950));
  };
  pathTimers.push(setTimeout(start, added ? 700 : 0));
  writeHash();
}
$('#path-result').addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-go]');
  if (b) { setView('graph'); goTo(b.dataset.go); }
});

// ---- theme toggle ----------------------------------------------------------------
$('#theme-toggle').addEventListener('click', () => {
  const dark = getComputedStyle(document.documentElement).getPropertyValue('color-scheme').trim() === 'dark';
  const next = dark ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  store.set('mythographia-theme', next);
  graph.refreshTheme();
});
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => graph.refreshTheme());

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!selected || view === 'timeline') return;
    renderDetail(selected);
    graph.flyTo(selected, { duration: 500 });
  }, 250);
});

// ---- routing -------------------------------------------------------------------
function writeHash() {
  let h = `#${view}`;
  if (view === 'path' && pathEnds.from && pathEnds.to) h += `/${pathEnds.from}/${pathEnds.to}`;
  else if (selected) h += `/${selected}`;
  if (location.hash !== h) history.replaceState(null, '', h);
}
function readHash() {
  const [v, a, b] = location.hash.slice(1).split('/').map(decodeURIComponent);
  if (v === 'path' && a && b && index.byId.has(a) && index.byId.has(b)) {
    pathEnds.from = a;
    pathEnds.to = b;
    $('#path-from').value = index.byId.get(a).title;
    $('#path-to').value = index.byId.get(b).title;
    setView('path', { push: false });
    setTimeout(runPath, 1300);
    return;
  }
  if (v === 'timeline' || v === 'graph' || v === 'path') setView(v, { push: false });
  if (a && index.byId.has(a)) setTimeout(() => goTo(a), 1300);
}
readHash();

// Hooks for the Playwright QA script (read-only views of state).
window.__myth = {
  index,
  graph,
  get timeline() { return timeline; },
  shortestPath: (a, b, o) => shortestPath(index, a, b, o),
  state: () => ({ view, selected, pathEnds: { ...pathEnds } }),
};
