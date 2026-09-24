// Pure graph model shared by the browser app, the ingester and the tests.
// No DOM, no network: everything here is deterministic over the dataset.

// Eight subtypes, but only four visual families: eight hues scattered across a
// graph cannot all be told apart (validated all-pairs: three hues pass). The
// subtype is always shown in text; colour carries the family, shape marks events.
export const FAMILIES = {
  divine: { label: 'Gods', order: 0 },
  mortal: { label: 'Mortals', order: 1 },
  monster: { label: 'Monsters', order: 2 },
  event: { label: 'Events', order: 3 },
};

export const NODE_TYPES = {
  primordial: { label: 'Primordial', family: 'divine', order: 0 },
  titan: { label: 'Titan', family: 'divine', order: 1 },
  olympian: { label: 'Olympian', family: 'divine', order: 2 },
  deity: { label: 'Deity', family: 'divine', order: 3 },
  hero: { label: 'Hero', family: 'mortal', order: 4 },
  mortal: { label: 'Mortal', family: 'mortal', order: 5 },
  creature: { label: 'Creature', family: 'monster', order: 6 },
  event: { label: 'Event', family: 'event', order: 7 },
};

export const familyOf = (node) => NODE_TYPES[node.type]?.family ?? 'mortal';

// rel: how the edge reads from s to t.
export const RELATIONS = {
  parent: { forward: 'parent of', backward: 'child of', family: true },
  consort: { forward: 'consort of', backward: 'consort of', family: true },
  sibling: { forward: 'sibling of', backward: 'sibling of', family: true },
  participant: { forward: 'appears in', backward: 'features', family: false },
  associated: { forward: 'linked with', backward: 'linked with', family: false },
};

export function buildIndex(data) {
  const byId = new Map();
  const adj = new Map();
  for (const n of data.nodes) {
    byId.set(n.id, n);
    adj.set(n.id, []);
  }
  const edges = [];
  for (const e of data.edges) {
    if (!byId.has(e.s) || !byId.has(e.t) || e.s === e.t) continue;
    edges.push(e);
    adj.get(e.s).push({ id: e.t, rel: e.rel, dir: 1, edge: e });
    adj.get(e.t).push({ id: e.s, rel: e.rel, dir: -1, edge: e });
  }
  const degree = new Map([...adj].map(([id, list]) => [id, new Set(list.map((x) => x.id)).size]));
  return { byId, adj, edges, degree, nodes: data.nodes };
}

export function relationLabel(step) {
  const r = RELATIONS[step.rel];
  if (!r) return step.rel;
  return step.dir === 1 ? r.forward : r.backward;
}

// Unweighted shortest path (BFS). Returns [{id, via}] where via describes the
// edge used to arrive at id (null for the start), or null when unreachable.
// Ties break deterministically on neighbour id so results are reproducible.
export function shortestPath(index, from, to, { exclude = new Set() } = {}) {
  if (!index.byId.has(from) || !index.byId.has(to)) return null;
  if (from === to) return [{ id: from, via: null }];
  const prev = new Map([[from, null]]);
  let frontier = [from];
  while (frontier.length) {
    const next = [];
    for (const u of frontier) {
      const nbrs = [...index.adj.get(u)].sort((a, b) => cmp(a.id, b.id) || cmp(a.rel, b.rel));
      for (const step of nbrs) {
        if (prev.has(step.id) || exclude.has(step.rel)) continue;
        prev.set(step.id, { from: u, rel: step.rel, dir: step.dir });
        if (step.id === to) return unwind(prev, to);
        next.push(step.id);
      }
    }
    frontier = next;
  }
  return null;
}

function unwind(prev, to) {
  const out = [];
  let cur = to;
  while (cur !== undefined) {
    const p = prev.get(cur);
    out.push({ id: cur, via: p ? { rel: p.rel, dir: p.dir } : null });
    if (!p) break;
    cur = p.from;
  }
  return out.reverse();
}

function cmp(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

export function neighbours(index, id) {
  return new Set((index.adj.get(id) || []).map((x) => x.id));
}

// Relative mythic chronology.
//
// Myth has no calendar, so the timeline axis is derived, not asserted: every
// parent edge says "child is one generation after parent", consorts and
// siblings say "same generation", and an event sits at the generation of the
// figures Wikipedia names in it. We solve those soft constraints by least
// squares (Gauss-Seidel) on the largest connected constraint component and
// leave everything else unplaced (null) rather than guess.
export function computeChronology(nodes, edges, { iterations = 800 } = {}) {
  const ids = nodes.map((n) => n.id);
  const cons = new Map(ids.map((id) => [id, []])); // id -> [{other, offset, w}]
  const add = (a, b, offset, w) => {
    // constraint: g[a] ≈ g[b] + offset
    cons.get(a).push({ other: b, offset, w });
    cons.get(b).push({ other: a, offset: -offset, w });
  };
  for (const e of edges) {
    if (!cons.has(e.s) || !cons.has(e.t) || e.s === e.t) continue;
    if (e.rel === 'parent') add(e.t, e.s, 1, 1);
    else if (e.rel === 'consort') add(e.s, e.t, 0, 0.6);
    else if (e.rel === 'sibling') add(e.s, e.t, 0, 0.6);
    else if (e.rel === 'participant') add(e.s, e.t, 0, 0.35);
  }

  // Largest connected component of the constraint graph.
  const comp = new Map();
  let best = null;
  for (const id of ids) {
    if (comp.has(id) || cons.get(id).length === 0) continue;
    const members = [id];
    comp.set(id, members);
    for (let i = 0; i < members.length; i++) {
      for (const c of cons.get(members[i])) {
        if (!comp.has(c.other)) {
          comp.set(c.other, members);
          members.push(c.other);
        }
      }
    }
    if (!best || members.length > best.length) best = members;
  }
  const result = new Map(ids.map((id) => [id, null]));
  if (!best || best.length < 2) return result;

  // Initialise by BFS so Gauss-Seidel starts near a solution.
  const g = new Map([[best[0], 0]]);
  const queue = [best[0]];
  while (queue.length) {
    const u = queue.shift();
    for (const c of cons.get(u)) {
      if (!g.has(c.other)) {
        g.set(c.other, g.get(u) - c.offset);
        queue.push(c.other);
      }
    }
  }
  for (let it = 0; it < iterations; it++) {
    for (const id of best) {
      let num = 0;
      let den = 0;
      for (const c of cons.get(id)) {
        num += c.w * (g.get(c.other) + c.offset);
        den += c.w;
      }
      g.set(id, num / den);
    }
  }
  const min = Math.min(...best.map((id) => g.get(id)));
  for (const id of best) result.set(id, Math.round((g.get(id) - min) * 10) / 10);
  return result;
}
