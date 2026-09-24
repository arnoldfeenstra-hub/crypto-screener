import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildIndex, shortestPath, computeChronology, relationLabel } from '../public/js/model.js';

const g = {
  nodes: ['a', 'b', 'c', 'd', 'e', 'x', 'lonely', 'iso1', 'iso2'].map((id) => ({ id, title: id })),
  edges: [
    { s: 'a', t: 'b', rel: 'parent' },
    { s: 'b', t: 'c', rel: 'parent' },
    { s: 'c', t: 'd', rel: 'consort' },
    { s: 'x', t: 'd', rel: 'participant' },
    { s: 'a', t: 'e', rel: 'associated' },
    { s: 'e', t: 'd', rel: 'associated' },
    { s: 'iso1', t: 'iso2', rel: 'parent' },
  ],
};
const idx = buildIndex(g);

test('shortestPath finds a minimal path with edge directions', () => {
  const p = shortestPath(idx, 'a', 'd');
  assert.equal(p.length, 3); // a - e - d beats a - b - c - d
  assert.deepEqual(p.map((s) => s.id), ['a', 'e', 'd']);
  const q = shortestPath(idx, 'c', 'a');
  assert.deepEqual(q.map((s) => s.id), ['c', 'b', 'a']);
  assert.equal(relationLabel(q[1].via), 'child of');
});

test('shortestPath respects excluded relations and unreachable pairs', () => {
  const p = shortestPath(idx, 'a', 'd', { exclude: new Set(['associated', 'participant']) });
  assert.deepEqual(p.map((s) => s.id), ['a', 'b', 'c', 'd']);
  assert.equal(shortestPath(idx, 'a', 'lonely'), null);
  assert.equal(shortestPath(idx, 'a', 'x', { exclude: new Set(['participant', 'associated']) }), null);
  assert.deepEqual(shortestPath(idx, 'a', 'a').map((s) => s.id), ['a']);
});

test('chronology orders generations and leaves the unconnected unplaced', () => {
  const gen = computeChronology(g.nodes, g.edges);
  assert.ok(gen.get('b') > gen.get('a'));
  assert.ok(gen.get('c') > gen.get('b'));
  assert.ok(Math.abs(gen.get('c') - gen.get('d')) < 0.5, 'consorts share a generation');
  assert.equal(gen.get('lonely'), null);
  assert.equal(gen.get('iso1'), null, 'only the largest component is placed');
  assert.equal(Math.min(...[...gen.values()].filter((v) => v != null)), 0);
});
