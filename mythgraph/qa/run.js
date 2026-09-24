// End-to-end QA with Playwright.  QA_URL=https://… npm run qa   (default: local dev server)
// Verifies search, both views, six-degrees correctness (against an independent
// BFS written here, not the app's own), frame times with the full dataset, and
// that the console stays clean. Screenshots land in qa/screenshots/.
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright')); } catch { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }

const BASE = process.env.QA_URL || 'http://localhost:4173/';
const OUT = new URL('./screenshots/', import.meta.url);
mkdirSync(OUT, { recursive: true });
const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
};
const shot = (page, name) => page.screenshot({ path: new URL(`${name}.png`, OUT).pathname });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('console', (m) => m.type() === 'error' && !/Failed to load resource.*(upload\.wikimedia|favicon)/.test(m.text()) && errors.push(m.text()));

const t0 = Date.now();
await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForFunction(() => window.__myth?.index, null, { timeout: 30000 });
check('app boots and loads /api/graph', true, `${Date.now() - t0} ms`);

// Independent copy of the data for correctness checks.
const data = await page.evaluate(() => fetch('/api/graph').then((r) => r.json()));
const adj = new Map(data.nodes.map((n) => [n.id, new Set()]));
for (const e of data.edges) { adj.get(e.s)?.add(e.t); adj.get(e.t)?.add(e.s); }
const bfsLen = (a, b) => {
  const dist = new Map([[a, 0]]);
  const q = [a];
  while (q.length) {
    const u = q.shift();
    if (u === b) return dist.get(u);
    for (const v of adj.get(u)) if (!dist.has(v)) { dist.set(v, dist.get(u) + 1); q.push(v); }
  }
  return null;
};
check('dataset non-empty', data.nodes.length > 0 && data.edges.length > 0, `${data.nodes.length} nodes, ${data.edges.length} edges`);

await sleep(3500);
await shot(page, '01-graph-core');

// Frame timing during wheel zoom + pan on the core view.
async function measureInteraction(label) {
  await page.evaluate(() => {
    window.__fps = [];
    let last = performance.now();
    const loop = (t) => { window.__fps.push(t - last); last = t; if (window.__fps.length < 400) requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  });
  const box = await page.locator('#graph-canvas').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 12; i++) { await page.mouse.wheel(0, i < 6 ? -120 : 120); await sleep(40); }
  await page.mouse.down();
  for (let i = 0; i < 30; i++) { await page.mouse.move(box.x + box.width / 2 + i * 8, box.y + box.height / 2 + i * 3); await sleep(16); }
  await page.mouse.up();
  await sleep(600);
  const { deltas, stats } = await page.evaluate(() => ({ deltas: window.__fps.slice(5), stats: window.__myth.graph.stats() }));
  const sorted = deltas.sort((a, b) => a - b);
  const p95 = sorted[Math.floor(sorted.length * 0.95)];
  const fps = 1000 / sorted[Math.floor(sorted.length / 2)];
  check(`${label}: interaction frame p95 < 34ms (≥30fps)`, p95 < 34, `median ${fps.toFixed(0)} fps, p95 frame ${p95.toFixed(1)} ms, draw p95 ${stats.frameMsP95?.toFixed(1)} ms, ${stats.nodes} nodes / ${stats.links} links`);
}
await measureInteraction('core web');

// Search flies to a node and selects it.
const target = data.nodes.filter((n) => n.type !== 'event').sort((a, b) => a.title.localeCompare(b.title))[Math.floor(data.nodes.length / 3)];
await page.fill('#search', target.title.slice(0, Math.min(target.title.length, 14)));
await page.waitForSelector('#search-results:not([hidden]) li');
const firstHit = await page.locator('#search-results li').first().getAttribute('data-id');
await page.locator(`#search-results li[data-id="${firstHit}"]`).click();
await sleep(1500);
const st = await page.evaluate(() => window.__myth.state());
check('search selects the chosen node', st.selected === firstHit, firstHit);
check('detail panel opens with title', await page.locator('#detail h2').textContent() === data.nodes.find((n) => n.id === firstHit).title);
const onScreen = await page.evaluate((id) => {
  const p = window.__myth.graph.screenPos(id);
  const c = document.querySelector('#graph-canvas').getBoundingClientRect();
  return p && p[0] > 0 && p[1] > 0 && p[0] < c.width && p[1] < c.height;
}, firstHit);
check('search flies the camera to the node', onScreen);
await shot(page, '02-search-focus');

// Hover card
const pos = await page.evaluate((id) => window.__myth.graph.screenPos(id), firstHit);
const cbox = await page.locator('#graph-canvas').boundingBox();
await page.mouse.move(cbox.x + pos[0], cbox.y + pos[1]);
await sleep(300);
check('hover card appears over a node', await page.locator('#hovercard').isVisible());
await shot(page, '03-hover');

// Double-click expands the neighbourhood.
const before = await page.evaluate(() => window.__myth.graph.visibleIds().length);
await page.mouse.dblclick(cbox.x + pos[0], cbox.y + pos[1]);
await sleep(800);
const after = await page.evaluate(() => window.__myth.graph.visibleIds().length);
const neighboursVisible = await page.evaluate((id) => [...window.__myth.index.adj.get(id)].every((s) => window.__myth.graph.onStage(s.id)), firstHit);
check('double-click expands neighbourhood', neighboursVisible, `${before} → ${after} visible`);

// Full dataset
await page.click('#show-all');
await sleep(4000);
await page.click('#detail .close').catch(() => {});
await shot(page, '04-graph-all');
await measureInteraction('full web');

// Six degrees: several random pairs, compared with the independent BFS.
await page.click('button[data-view="path"]');
const people = data.nodes.filter((n) => n.type !== 'event');
let pathOk = 0;
let tried = 0;
for (let i = 0; tried < 5 && i < 40; i++) {
  const a = people[(i * 37 + 11) % people.length];
  const b = people[(i * 91 + 5) % people.length];
  if (a.id === b.id) continue;
  const expected = bfsLen(a.id, b.id);
  if (expected == null || expected < 2) continue;
  tried++;
  for (const [sel, n] of [['#path-from', a], ['#path-to', b]]) {
    await page.fill(sel, n.title);
    await page.waitForSelector('#path-suggest:not([hidden]) li');
    await page.locator(`#path-suggest li[data-id="${n.id}"]`).click();
  }
  await sleep(300);
  await page.click('#path-go');
  await sleep(400);
  const chain = await page.$$eval('#path-result button.node', (els) => els.map((e) => e.dataset.go));
  const valid = chain[0] === a.id && chain.at(-1) === b.id && chain.length - 1 === expected
    && chain.every((id, k) => k === 0 || adj.get(chain[k - 1]).has(id));
  if (valid) pathOk++;
  else console.log('   path mismatch', a.id, b.id, 'expected', expected, 'got', chain);
}
check('six degrees returns a valid shortest path', tried > 0 && pathOk === tried, `${pathOk}/${tried} pairs match independent BFS`);
await sleep(3500);
await shot(page, '05-six-degrees');

// Timeline
await page.click('button[data-view="timeline"]');
await sleep(1200);
const dots = await page.locator('#timeline-svg .tl-dot').count();
check('timeline renders every node', dots === data.nodes.length, `${dots} dots`);
await shot(page, '06-timeline');
const tstats = await page.evaluate(() => window.__myth.timeline.stats());
await page.evaluate((v) => { const s = document.querySelector('#scrub'); s.value = v; s.dispatchEvent(new Event('input')); }, String(tstats.maxGen / 2));
await sleep(400);
const faded = await page.$$eval('#timeline-svg .tl-dot', (els) => els.filter((e) => Number(e.getAttribute('opacity')) < 0.2).length);
check('scrubbing fades the future', faded > 0, `${faded} dots faded at generation ${(tstats.maxGen / 2).toFixed(1)}`);
await shot(page, '07-timeline-scrubbed');
const hub = [...data.nodes].filter((n) => n.generation != null).sort((a, b) => adj.get(b.id).size - adj.get(a.id).size)[0];
await page.locator(`#timeline-svg .tl-dot`).nth(data.nodes.findIndex((n) => n.id === hub.id)).click({ force: true });
await sleep(700);
const threads = await page.locator('#timeline-svg .tl-thread').count();
check('selecting on timeline draws its thread', threads === adj.get(hub.id).size, `${threads} threads for ${hub.title}`);
await shot(page, '08-timeline-thread');
await page.click('#play');
await sleep(1500);
check('play advances the scrubber', Number(await page.inputValue('#scrub')) < tstats.maxGen);
await page.click('#play');

// Dark mode + mobile
await page.click('#theme-toggle');
await page.click('button[data-view="graph"]');
await sleep(1200);
await shot(page, '09-dark');
await page.click('#theme-toggle');
await page.setViewportSize({ width: 390, height: 844 });
await sleep(1200);
const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
check('no horizontal overflow at phone width', !overflow);
await shot(page, '10-mobile');

check('no console errors', errors.length === 0, errors.slice(0, 3).join(' | '));
await browser.close();
writeFileSync(new URL('./last-run.json', import.meta.url), JSON.stringify({ base: BASE, at: new Date().toISOString(), results }, null, 2));
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
