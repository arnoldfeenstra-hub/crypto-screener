// Minimal MediaWiki Action API client for en.wikipedia.org.
// Polite by construction: identifying User-Agent, maxlag, low concurrency,
// exponential backoff on 429/5xx.

const API = 'https://en.wikipedia.org/w/api.php';
const UA =
  'MythGraph/0.1 (educational knowledge graph; https://github.com/arnoldfeenstra-hub/crypto-screener)';

let active = 0;
const waiting = [];
const MAX_CONCURRENCY = 3;

async function slot() {
  if (active < MAX_CONCURRENCY) {
    active++;
    return;
  }
  await new Promise((r) => waiting.push(r));
  active++;
}
function release() {
  active--;
  const next = waiting.shift();
  if (next) next();
}

export async function api(params, { attempt = 0 } = {}) {
  const body = new URLSearchParams({ format: 'json', formatversion: '2', maxlag: '5', ...params });
  await slot();
  let res;
  try {
    res = await fetch(`${API}?${body}`, { headers: { 'User-Agent': UA, 'Api-User-Agent': UA } });
  } catch (err) {
    release();
    if (attempt < 5) return retry(params, attempt, err.message);
    throw err;
  }
  release();
  if (res.status === 429 || res.status >= 500) {
    if (attempt < 5) return retry(params, attempt, `HTTP ${res.status}`);
    throw new Error(`Wikipedia API ${res.status}`);
  }
  if (!res.ok) throw new Error(`Wikipedia API ${res.status}`);
  const json = await res.json();
  if (json.error) {
    if (json.error.code === 'maxlag' && attempt < 5) return retry(params, attempt, 'maxlag');
    throw new Error(`Wikipedia API error ${json.error.code}: ${json.error.info}`);
  }
  return json;
}

async function retry(params, attempt, why) {
  const ms = 1000 * 2 ** attempt;
  console.warn(`  retrying in ${ms}ms (${why})`);
  await new Promise((r) => setTimeout(r, ms));
  return api(params, { attempt: attempt + 1 });
}

// Runs a query over titles in batches, following `continue` so no page loses
// props. Returns pages keyed by canonical title, plus the normalisation and
// redirect maps so callers can resolve what they asked for.
export async function queryTitles(titles, params, batchSize = 50) {
  const pages = new Map();
  const resolved = new Map(); // requested title -> canonical title
  for (let i = 0; i < titles.length; i += batchSize) {
    const batch = titles.slice(i, i + batchSize);
    let cont = {};
    const alias = new Map(batch.map((t) => [t, t]));
    do {
      const json = await api({ action: 'query', titles: batch.join('|'), redirects: '1', ...params, ...cont });
      const q = json.query || {};
      for (const n of q.normalized || []) for (const [k, v] of alias) if (v === n.from) alias.set(k, n.to);
      for (const r of q.redirects || []) for (const [k, v] of alias) if (v === r.from) alias.set(k, r.to);
      for (const p of q.pages || []) {
        const prev = pages.get(p.title) || {};
        pages.set(p.title, mergePage(prev, p));
      }
      cont = json.continue || null;
    } while (cont);
    for (const [k, v] of alias) resolved.set(k, v);
  }
  return { pages, resolved };
}

function mergePage(a, b) {
  const out = { ...a, ...b };
  for (const key of ['categories', 'images', 'redirects', 'revisions', 'imageinfo']) {
    if (a[key] || b[key]) out[key] = [...(a[key] || []), ...(b[key] || [])];
  }
  return out;
}

export async function pMap(items, fn, concurrency = 3) {
  const out = new Array(items.length);
  let i = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (i < items.length) {
      const idx = i++;
      out[idx] = await fn(items[idx], idx);
    }
  });
  await Promise.all(workers);
  return out;
}
