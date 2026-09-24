// Wikipedia -> Neon ingest.
//
//   npm run ingest                 fetch everything, write to Postgres
//   npm run ingest -- --out f.json also (or with --no-db, only) write the graph JSON
//
// Nothing is generated: text, images, image metadata and relationships are
// all taken from en.wikipedia.org / commons.wikimedia.org responses.

import { writeFileSync } from 'node:fs';
import pg from 'pg';
import { api, queryTitles, pMap } from './wiki.js';
import {
  parseInfobox, familyFromInfobox, leadSection, stripRefs, extractLinks, imageCandidates,
  imageRecord, pickFunFact, slugify, typeFromDescription, normalizeTitle,
} from './parse.js';
import { SEEDS } from './seeds.js';
import { computeChronology } from '../public/js/model.js';
import { databaseUrl } from '../lib/env.js';
import { toApiGraph } from '../lib/graph.js';

const args = process.argv.slice(2);
const outPath = args.includes('--out') ? args[args.indexOf('--out') + 1] : null;
const writeDb = !args.includes('--no-db');
const MAX_EXPANSION = 220;

const report = { missing: [], disambiguation: [], duplicates: [], expanded: [], skippedExpansion: 0, noImage: [] };
const log = (...a) => console.log(...a);

// 1. Resolve seed titles to canonical pages -------------------------------
log(`Resolving ${SEEDS.length} seed titles…`);
const seedTitles = SEEDS.map((s) => s.title);
const seedRes = await queryTitles(seedTitles, { prop: 'info|pageprops|description', ppprop: 'disambiguation' });
const nodes = new Map(); // canonical title -> node draft
for (const seed of SEEDS) {
  const canonical = seedRes.resolved.get(seed.title);
  const page = seedRes.pages.get(canonical);
  if (!page || page.missing || page.invalid) { report.missing.push(seed.title); continue; }
  if (page.pageprops && 'disambiguation' in page.pageprops) { report.disambiguation.push(seed.title); continue; }
  if (nodes.has(page.title)) { report.duplicates.push(seed.title); continue; }
  nodes.set(page.title, { title: page.title, type: seed.type, type_source: 'seed', description: page.description ?? null });
}
log(`  ${nodes.size} resolved; missing ${report.missing.length}, disambiguation ${report.disambiguation.length}`);

// 2. Wikitext (infobox, lead links, image candidates) ---------------------
async function fetchWikitext(titles) {
  const { pages } = await queryTitles(titles, { prop: 'revisions', rvprop: 'content|ids', rvslots: 'main' }, 10);
  for (const p of pages.values()) {
    const n = nodes.get(p.title);
    const rev = p.revisions?.[0];
    if (n && rev) {
      n.wikitext = rev.slots?.main?.content ?? rev.content ?? '';
      n.revision_id = rev.revid;
    }
  }
}
log('Fetching wikitext…');
await fetchWikitext([...nodes.keys()]);

// 3. One-hop expansion via infobox family links ---------------------------
const familyTargets = new Set();
for (const n of nodes.values()) {
  for (const f of familyFromInfobox(parseInfobox(n.wikitext || ''))) familyTargets.add(f.target);
}
const unknown = [...familyTargets].filter((t) => !nodes.has(t));
log(`Expanding via ${unknown.length} infobox family links…`);
const expRes = await queryTitles(unknown, { prop: 'info|pageprops|description', ppprop: 'disambiguation' });
const newTitles = [];
for (const t of unknown) {
  const canonical = expRes.resolved.get(t);
  const page = expRes.pages.get(canonical);
  if (!page || page.missing || nodes.has(page.title) || newTitles.includes(page.title)) continue;
  if (page.pageprops && 'disambiguation' in page.pageprops) continue;
  const type = typeFromDescription(page.description);
  if (!type) { report.skippedExpansion++; continue; }
  if (newTitles.length >= MAX_EXPANSION) break;
  newTitles.push(page.title);
  nodes.set(page.title, { title: page.title, type, type_source: 'description', description: page.description });
  report.expanded.push(`${page.title} [${type}] — "${page.description}"`);
}
log(`  added ${newTitles.length}`);
if (newTitles.length) await fetchWikitext(newTitles);

// 4. Lead extracts + page images --------------------------------------------
log('Fetching lead extracts and page images…');
const titles = [...nodes.keys()];
const ex = await queryTitles(
  titles,
  { prop: 'extracts|pageimages|description', exintro: '1', explaintext: '1', exlimit: '20', piprop: 'name', pilimit: '20' },
  20,
);
for (const p of ex.pages.values()) {
  const n = nodes.get(p.title);
  if (!n) continue;
  n.extract = p.extract?.trim() || null;
  n.description = p.description ?? n.description ?? null;
  n.pageimage = p.pageimage ? `File:${normalizeTitle(p.pageimage)}` : null;
}

// 5. Fun facts: verbatim opening of a named section --------------------------
log('Fetching full plaintext for fun facts…');
await pMap(titles, async (title) => {
  const json = await api({ action: 'query', prop: 'extracts', explaintext: '1', exsectionformat: 'wiki', titles: title });
  const text = json.query?.pages?.[0]?.extract;
  nodes.get(title).funFact = pickFunFact(text);
});

// 6. Redirect aliases so links resolve to canonical titles -------------------
log('Resolving redirect aliases…');
const alias = new Map(titles.map((t) => [t, t]));
const rd = await queryTitles(titles, { prop: 'redirects', rdlimit: 'max', rdnamespace: '0', rdprop: 'title' });
for (const p of rd.pages.values()) for (const r of p.redirects || []) alias.set(r.title, p.title);
const resolve = (t) => alias.get(t) ?? null;

// 7. Edges -------------------------------------------------------------------
log('Building edges…');
const edgeMap = new Map();
const addEdge = (s, t, rel, provenance) => {
  if (!s || !t || s === t) return;
  // consort/sibling are symmetric: store once, ordered
  if (rel === 'consort' || rel === 'sibling' || rel === 'associated') [s, t] = s < t ? [s, t] : [t, s];
  const key = `${s}\u0000${t}\u0000${rel}`;
  if (!edgeMap.has(key)) edgeMap.set(key, { s, t, rel, provenance });
};
const isEvent = (title) => nodes.get(title)?.type === 'event';
const leadLinks = new Map();
for (const n of nodes.values()) {
  const text = n.wikitext || '';
  for (const f of familyFromInfobox(parseInfobox(text))) {
    const other = resolve(f.target);
    if (!other) continue;
    if (isEvent(n.title) || isEvent(other)) continue;
    const prov = `infobox:${f.param}@${n.title}`;
    if (f.rel === 'parent') f.dir === 'in' ? addEdge(other, n.title, 'parent', prov) : addEdge(n.title, other, 'parent', prov);
    else addEdge(n.title, other, f.rel, prov);
  }
  const links = new Set(extractLinks(leadSection(stripRefs(text))).map(resolve).filter(Boolean));
  links.delete(n.title);
  leadLinks.set(n.title, links);
}
// parent beats consort/sibling for the same pair (e.g. Gaia–Uranus is both);
// keep both, the UI draws one line per pair and lists all relations.
const familyPairs = new Set([...edgeMap.values()].map((e) => [e.s, e.t].sort().join('\u0000')));
for (const [title, links] of leadLinks) {
  for (const other of links) {
    const prov = `lead-link@${title}`;
    if (isEvent(title) && !isEvent(other)) addEdge(other, title, 'participant', prov);
    else if (!isEvent(title) && isEvent(other)) addEdge(title, other, 'participant', prov);
    else if (isEvent(title) && isEvent(other)) addEdge(title, other, 'associated', prov);
    else if (leadLinks.get(other)?.has(title) && !familyPairs.has([title, other].sort().join('\u0000'))) {
      addEdge(title, other, 'associated', `mutual-lead-link@${title}+${other}`);
    }
  }
}
log(`  ${edgeMap.size} edges`);

// 8. Images: first public-domain candidate per page --------------------------
log('Checking image licences on Commons…');
const candidates = new Map();
for (const n of nodes.values()) {
  const list = [n.pageimage, ...imageCandidates(n.wikitext || '')].filter(Boolean);
  candidates.set(n.title, [...new Set(list)].slice(0, 6));
}
const allFiles = [...new Set([...candidates.values()].flat())];
const info = await queryTitles(allFiles, {
  prop: 'imageinfo',
  iiprop: 'url|extmetadata|size',
  iiurlwidth: '500',
  iiextmetadatafilter: 'License|LicenseShortName|Artist|DateTimeOriginal|Credit|ObjectName|ImageDescription',
});
const fileRecord = new Map();
for (const f of allFiles) {
  const page = info.pages.get(info.resolved.get(f));
  const rec = imageRecord(page);
  if (rec && rec.width >= 240) fileRecord.set(f, rec);
}
for (const n of nodes.values()) {
  const f = candidates.get(n.title).find((c) => fileRecord.has(c));
  n.image = f ? fileRecord.get(f) : null;
  if (!n.image) report.noImage.push(n.title);
}
log(`  ${titles.length - report.noImage.length}/${titles.length} nodes have a public-domain image`);

// 9. Relative chronology ----------------------------------------------------
const idOf = new Map(titles.map((t) => [t, slugify(t)]));
const edges = [...edgeMap.values()].map((e) => ({ ...e, s: idOf.get(e.s), t: idOf.get(e.t) }));
const chronoNodes = titles.map((t) => ({ id: idOf.get(t) }));
const generation = computeChronology(chronoNodes, edges);

const graph = {
  nodes: titles.map((t) => {
    const n = nodes.get(t);
    return {
      id: idOf.get(t), title: t, type: n.type, type_source: n.type_source, description: n.description,
      extract: n.extract, fun_fact: n.funFact?.text ?? null, fun_fact_section: n.funFact?.section ?? null,
      generation: generation.get(idOf.get(t)),
      wiki_url: `https://en.wikipedia.org/wiki/${encodeURIComponent(t.replace(/ /g, '_'))}`,
      revision_id: n.revision_id ?? null, image: n.image,
    };
  }),
  edges,
};
const dupIds = titles.length - new Set(idOf.values()).size;
if (dupIds) throw new Error(`${dupIds} slug collisions`);

if (outPath) {
  const rows = graph.nodes.map((n) => ({ ...n, ...(n.image || {}) }));
  const edgeRows = graph.edges.map((e) => ({ source: e.s, target: e.t, rel: e.rel, provenance: e.provenance }));
  writeFileSync(outPath, JSON.stringify(toApiGraph(rows, edgeRows, new Date().toISOString())));
  log(`Wrote ${outPath}`);
}

// 10. Write to Postgres in one transaction ----------------------------------
if (writeDb) {
  log('Writing to Postgres…');
  const client = new pg.Client({ connectionString: databaseUrl(), ssl: { rejectUnauthorized: true } });
  await client.connect();
  try {
    await client.query('begin');
    const run = await client.query('insert into ingest_runs (seed_count) values ($1) returning id', [SEEDS.length]);
    const runId = run.rows[0].id;
    await client.query('delete from edges');
    await client.query('delete from images');
    await client.query('delete from nodes');
    for (const n of graph.nodes) {
      await client.query(
        `insert into nodes (id, title, type, type_source, description, extract, fun_fact, fun_fact_section,
                            generation, wiki_url, revision_id, run_id)
         values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`,
        [n.id, n.title, n.type, n.type_source, n.description, n.extract, n.fun_fact, n.fun_fact_section,
          n.generation, n.wiki_url, n.revision_id, runId],
      );
      const i = n.image;
      if (i) {
        await client.query(
          `insert into images (node_id, file_title, thumb_url, thumb_width, thumb_height, original_url, commons_url,
                               artist, date_text, license, credit, object_name)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`,
          [n.id, i.file_title, i.thumb_url, i.thumb_width, i.thumb_height, i.original_url, i.commons_url,
            i.artist, i.date_text, i.license, i.credit, i.object_name],
        );
      }
    }
    for (const e of graph.edges) {
      await client.query('insert into edges (source, target, rel, provenance) values ($1,$2,$3,$4)', [e.s, e.t, e.rel, e.provenance]);
    }
    await client.query(
      `update ingest_runs set finished_at = now(), node_count = $2, edge_count = $3, image_count = $4, report = $5 where id = $1`,
      [runId, graph.nodes.length, graph.edges.length, graph.nodes.filter((n) => n.image).length, report],
    );
    await client.query('commit');
  } catch (err) {
    await client.query('rollback');
    throw err;
  } finally {
    await client.end();
  }
}

writeFileSync(new URL('./last-report.json', import.meta.url), JSON.stringify(report, null, 2));
log(`Done: ${graph.nodes.length} nodes, ${graph.edges.length} edges. Report: ingest/last-report.json`);
