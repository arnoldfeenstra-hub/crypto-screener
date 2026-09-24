// Synthetic stress graph for QA ONLY. Labels are deliberately artificial
// ("Synthetic figure 017") so it can never be mistaken for mythology content.
// It is never deployed (.vercelignore) and never written to the database.
import { writeFileSync } from 'node:fs';
import { computeChronology } from '../public/js/model.js';

let seed = 7;
const rand = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
const pick = (a) => a[Math.floor(rand() * a.length)];
const N = Number(process.argv[2] || 520);
const types = ['primordial', 'titan', 'olympian', 'deity', 'hero', 'mortal', 'creature'];
const nodes = [];
const edges = [];
const byGen = [];
for (let i = 0; i < N; i++) {
  const isEvent = i % 12 === 11;
  const g = Math.min(9, Math.floor((i / N) * 10));
  const type = isEvent ? 'event' : types[Math.min(types.length - 1, Math.floor(g * 0.75 + rand() * 1.5))];
  const id = `synthetic-${String(i).padStart(3, '0')}`;
  const n = {
    id, title: `Synthetic ${isEvent ? 'event' : 'figure'} ${String(i).padStart(3, '0')}`, type,
    description: `QA fixture node (${type})`, extract: 'Synthetic QA text. Not mythology content.',
    funFact: i % 3 ? null : { text: 'Synthetic fixture sentence for layout testing.', section: 'Fixture' },
    url: 'https://example.invalid/', revision: null, image: null,
  };
  nodes.push(n);
  if (isEvent) {
    const k = 3 + Math.floor(rand() * 9);
    for (let j = 0; j < k && nodes.length > 2; j++) edges.push({ s: pick(nodes.slice(Math.max(0, i - 80), i)).id, t: id, rel: 'participant' });
    continue;
  }
  (byGen[g] ||= []).push(id);
  if (g > 0 && byGen[g - 1]?.length) {
    const p1 = pick(byGen[g - 1]);
    edges.push({ s: p1, t: id, rel: 'parent' });
    if (rand() < 0.6) {
      const p2 = pick(byGen[g - 1]);
      if (p2 !== p1) { edges.push({ s: p2, t: id, rel: 'parent' }); edges.push({ s: p1, t: p2, rel: 'consort' }); }
    }
  }
  if (rand() < 0.2 && nodes.length > 5) edges.push({ s: id, t: pick(nodes.slice(0, i)).id, rel: 'associated' });
}
const seen = new Set();
const uniq = edges.filter((e) => e.s !== e.t && !seen.has(`${e.s}|${e.t}|${e.rel}`) && seen.add(`${e.s}|${e.t}|${e.rel}`));
const gen = computeChronology(nodes, uniq);
for (const n of nodes) n.generation = gen.get(n.id);
const out = new URL('./fixture-graph.json', import.meta.url);
writeFileSync(out, JSON.stringify({ meta: { source: 'SYNTHETIC QA FIXTURE', counts: { nodes: nodes.length, edges: uniq.length } }, nodes, edges: uniq }));
console.log(`fixture: ${nodes.length} nodes, ${uniq.length} edges`);
