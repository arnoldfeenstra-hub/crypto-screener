/* global d3 */
import { familyOf } from './model.js';

const REL_STYLE = {
  parent: { dist: 42, alpha: 0.34, width: 1, dash: null },
  consort: { dist: 34, alpha: 0.26, width: 1, dash: null },
  sibling: { dist: 48, alpha: 0.16, width: 0.8, dash: null },
  participant: { dist: 64, alpha: 0.2, width: 0.8, dash: [2.5, 3] },
  associated: { dist: 86, alpha: 0.1, width: 0.7, dash: null },
};
const GEN_SPACING = 70;
const HOP_MS = 520;

export function createGraphView({ canvas, index, onHover, onSelect, onExpand }) {
  const ctx = canvas.getContext('2d');
  let width = 0;
  let height = 0;
  let dpr = 1;
  let colors = readColors();
  let transform = d3.zoomIdentity;
  let insetRight = 0; // px covered by side panels, so "centre" means the visible centre
  let insetLeft = 0;
  let insetBottom = 0;

  const simNodes = new Map();
  let nodes = [];
  let links = [];
  let baseVisible = new Set();
  let hiddenFamilies = new Set();
  let focusId = null;
  let hoverId = null;
  let path = null; // { ids, start }
  let dirty = true;
  let lastFrameTimes = [];
  let dprCap = 2;
  let slowFrames = [];
  let prevFrameAt = 0;
  const imageCache = new Map();
  const textWidth = new Map();

  const maxGen = d3.max(index.nodes, (n) => n.generation) ?? 0;
  const hubRank = new Map([...index.nodes].sort((a, b) => index.degree.get(b.id) - index.degree.get(a.id)).map((n, i) => [n.id, i]));
  const radius = (id) => Math.min(20, 3.2 + 2.1 * Math.sqrt(index.degree.get(id) || 0));

  const sim = d3
    .forceSimulation()
    .alphaDecay(0.028)
    .velocityDecay(0.38)
    .force('link', d3.forceLink().id((d) => d.id))
    .force('charge', d3.forceManyBody().strength((d) => -70 - d.r * 9).distanceMax(520).theta(0.9))
    .force('collide', d3.forceCollide((d) => d.r + 3).strength(0.8))
    .force('x', d3.forceX(0).strength(0.012))
    .force(
      'y',
      d3
        .forceY((d) => (d.data.generation == null ? 0 : (d.data.generation - maxGen / 2) * GEN_SPACING))
        .strength((d) => (d.data.generation == null ? 0.02 : 0.07)),
    )
    .on('tick', () => (dirty = true));

  // ---- visibility -------------------------------------------------------------
  function coreSet() {
    const ranked = [...index.nodes].sort((a, b) => index.degree.get(b.id) - index.degree.get(a.id));
    const core = new Set(ranked.slice(0, 80).map((n) => n.id));
    for (const n of index.nodes) if (n.type === 'olympian') core.add(n.id);
    return core;
  }

  function applyVisible(originId = null) {
    const vis = [...baseVisible].filter((id) => !hiddenFamilies.has(familyOf(index.byId.get(id))));
    const visSet = new Set(vis);
    const origin = originId && simNodes.get(originId);
    let added = 0;
    nodes = vis.map((id) => {
      let n = simNodes.get(id);
      if (!n) {
        const data = index.byId.get(id);
        const g = data.generation == null ? 0 : (data.generation - maxGen / 2) * GEN_SPACING;
        const angle = Math.random() * Math.PI * 2;
        n = origin && origin.x != null
          ? { id, data, r: radius(id), x: origin.x + Math.cos(angle) * 12, y: origin.y + Math.sin(angle) * 12 }
          : { id, data, r: radius(id), x: (Math.random() - 0.5) * 400, y: g + (Math.random() - 0.5) * 60 };
        n.fade = 0;
        simNodes.set(id, n);
        added++;
      } else if (!n.onStage) {
        n.fade = 0;
        if (origin) { n.x = origin.x; n.y = origin.y; }
      }
      n.onStage = true;
      return n;
    });
    for (const n of simNodes.values()) if (!visSet.has(n.id)) n.onStage = false;
    links = index.edges
      .filter((e) => visSet.has(e.s) && visSet.has(e.t))
      .map((e) => ({ source: e.s, target: e.t, rel: e.rel }));
    sim.nodes(nodes);
    sim
      .force('link')
      .links(links)
      .distance((l) => REL_STYLE[l.rel]?.dist ?? 60)
      .strength((l) => {
        const k = 1 / Math.min(countLinks(l.source), countLinks(l.target));
        return l.rel === 'associated' ? k * 0.35 : l.rel === 'participant' ? k * 0.6 : k;
      });
    sim.alpha(Math.max(sim.alpha(), added ? 0.6 : 0.3)).restart();
    dirty = true;
  }
  const linkCount = () => {
    const m = new Map();
    for (const l of links) {
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      m.set(s, (m.get(s) || 0) + 1);
      m.set(t, (m.get(t) || 0) + 1);
    }
    return m;
  };
  let counts = new Map();
  function countLinks(n) {
    if (!counts.size) counts = linkCount();
    return counts.get(typeof n === 'object' ? n.id : n) || 1;
  }

  function setVisible(ids, originId) {
    baseVisible = new Set(ids);
    counts = new Map();
    applyVisible(originId);
  }

  // ---- geometry helpers -----------------------------------------------------
  function toGraph(px, py) {
    return transform.invert([px, py]);
  }
  function nodeAt(px, py) {
    const [x, y] = toGraph(px, py);
    const n = sim.find(x, y, 30 / transform.k + 20);
    if (!n) return null;
    const d = Math.hypot(n.x - x, n.y - y);
    return d <= n.r + 6 / transform.k ? n : null;
  }

  // ---- camera ------------------------------------------------------------------
  const zoom = d3
    .zoom()
    .scaleExtent([0.12, 7])
    .on('zoom', (ev) => {
      transform = ev.transform;
      dirty = true;
    });
  const sel = d3.select(canvas);

  function centreTransform(x, y, k) {
    const cx = insetLeft + (width - insetRight - insetLeft) / 2;
    return d3.zoomIdentity.translate(cx - x * k, (height - insetBottom) / 2 - y * k).scale(k);
  }
  function flyTo(id, { k, duration = 1000 } = {}) {
    const n = simNodes.get(id);
    if (!n) return;
    const scale = k ?? Math.max(transform.k, 1.5);
    sel.transition('camera').duration(duration).ease(d3.easeCubicInOut).call(zoom.transform, centreTransform(n.x, n.y, scale));
  }
  function fitTo(ids, { duration = 1000, pad = 80, maxK = 2.4 } = {}) {
    const pts = ids.map((id) => simNodes.get(id)).filter((n) => n && n.onStage);
    if (!pts.length) return;
    const [x0, x1] = d3.extent(pts, (n) => n.x);
    const [y0, y1] = d3.extent(pts, (n) => n.y);
    const w = Math.max(1, x1 - x0);
    const h = Math.max(1, y1 - y0);
    const k = Math.min(maxK, (width - insetLeft - insetRight - pad * 2) / w, (height - pad * 2) / h);
    sel.transition('camera').duration(duration).ease(d3.easeCubicInOut).call(zoom.transform, centreTransform((x0 + x1) / 2, (y0 + y1) / 2, k));
  }
  function fitAll(duration = 900) {
    fitTo(nodes.map((n) => n.id), { duration, pad: 40, maxK: 1.6 });
  }

  // ---- interaction -------------------------------------------------------------
  let dragMoved = false;
  const drag = d3
    .drag()
    .container(canvas)
    .clickDistance(4)
    .subject((ev) => {
      const n = nodeAt(ev.x, ev.y);
      if (!n) return null;
      const [x, y] = toGraph(ev.x, ev.y);
      n.dx = n.x - x;
      n.dy = n.y - y;
      return n;
    })
    .on('start', (ev) => {
      dragMoved = false;
      canvas.classList.add('dragging');
      sim.alphaTarget(0.25).restart();
      const n = ev.subject;
      n.fx = n.x;
      n.fy = n.y;
    })
    .on('drag', (ev) => {
      dragMoved = true;
      const [x, y] = toGraph(ev.x, ev.y);
      ev.subject.fx = x + ev.subject.dx;
      ev.subject.fy = y + ev.subject.dy;
    })
    .on('end', (ev) => {
      canvas.classList.remove('dragging');
      sim.alphaTarget(0);
      ev.subject.fx = null;
      ev.subject.fy = null;
    });
  sel.call(drag).call(zoom).on('dblclick.zoom', null);

  canvas.addEventListener('pointermove', (ev) => {
    if (ev.buttons) return;
    const rect = canvas.getBoundingClientRect();
    const n = nodeAt(ev.clientX - rect.left, ev.clientY - rect.top);
    const id = n ? n.id : null;
    canvas.classList.toggle('pointing', !!n);
    if (id !== hoverId) {
      hoverId = id;
      dirty = true;
    }
    onHover?.(id, ev.clientX - rect.left, ev.clientY - rect.top);
  });
  canvas.addEventListener('pointerleave', () => {
    hoverId = null;
    dirty = true;
    onHover?.(null);
  });
  canvas.addEventListener('click', (ev) => {
    if (dragMoved) { dragMoved = false; return; }
    const rect = canvas.getBoundingClientRect();
    const n = nodeAt(ev.clientX - rect.left, ev.clientY - rect.top);
    onSelect?.(n ? n.id : null);
  });
  canvas.addEventListener('dblclick', (ev) => {
    const rect = canvas.getBoundingClientRect();
    const n = nodeAt(ev.clientX - rect.left, ev.clientY - rect.top);
    if (n) onExpand?.(n.id);
    else sel.transition('camera').duration(500).call(zoom.scaleBy, 1.8, [ev.clientX - rect.left, ev.clientY - rect.top]);
  });

  // ---- rendering ---------------------------------------------------------------
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (k) => cs.getPropertyValue(k).trim();
    return {
      paper: v('--paper'), card: v('--card'), ink: v('--ink'), muted: v('--muted'), accent: v('--accent'),
      divine: v('--divine'), mortal: v('--mortal'), monster: v('--monster'), event: v('--event'),
    };
  }
  const famColor = (n) => colors[familyOf(n.data)];

  function resize() {
    const rect = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, dprCap);
    const had = width > 0 && height > 0;
    const dw = rect.width - width;
    const dh = rect.height - height;
    width = rect.width;
    height = rect.height;
    // keep whatever was centred still centred when the viewport changes
    if (had && (dw || dh) && width && height) sel.call(zoom.transform, transform.translate(dw / 2 / transform.k, dh / 2 / transform.k));
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    dirty = true;
  }

  function thumb(n) {
    const img = n.data.image;
    if (!img?.thumb) return null;
    const url = img.thumb.replace(/\/\d+px-/, '/120px-');
    let entry = imageCache.get(url);
    if (!entry) {
      const el = new Image();
      el.decoding = 'async';
      el.referrerPolicy = 'no-referrer';
      entry = { el, ok: false };
      el.onload = () => { entry.ok = true; dirty = true; };
      el.src = url;
      imageCache.set(url, entry);
    }
    return entry.ok ? entry.el : null;
  }

  function highlightSets() {
    const strong = new Set();
    const pathSet = path ? new Set(path.ids) : null;
    if (pathSet) for (const id of pathSet) strong.add(id);
    const center = focusId ?? (pathSet ? null : hoverId);
    if (center) {
      strong.add(center);
      for (const x of index.adj.get(center) || []) strong.add(x.id);
    }
    return { strong, active: pathSet != null || center != null, center, pathSet };
  }

  function draw(now) {
    const { strong, active, center, pathSet } = highlightSets();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.translate(transform.x, transform.y);
    ctx.scale(transform.k, transform.k);

    // eased per-node emphasis, so focus changes fade rather than snap
    let animating = false;
    for (const n of nodes) {
      const target = !active || strong.has(n.id) ? 1 : 0.14;
      n.em = n.em == null ? target : n.em + (target - n.em) * 0.18;
      if (Math.abs(n.em - target) > 0.01) animating = true;
      if (n.fade < 1) { n.fade = Math.min(1, n.fade + 0.06); animating = true; }
    }

    // edges, batched by style
    const lw = 1 / Math.sqrt(transform.k);
    for (const [rel, st] of Object.entries(REL_STYLE)) {
      ctx.beginPath();
      let any = false;
      for (const l of links) {
        if (l.rel !== rel) continue;
        const inc = center && (l.source.id === center || l.target.id === center);
        if (inc) continue;
        curve(l.source, l.target);
        any = true;
      }
      if (!any) continue;
      ctx.setLineDash(st.dash ? st.dash.map((d) => d * lw) : []);
      ctx.strokeStyle = colors.ink;
      ctx.globalAlpha = st.alpha * (active ? 0.28 : 1);
      ctx.lineWidth = st.width * lw;
      ctx.stroke();
    }
    if (center) {
      const c = simNodes.get(center);
      for (const l of links) {
        if (l.source.id !== center && l.target.id !== center) continue;
        const st = REL_STYLE[l.rel] || REL_STYLE.associated;
        ctx.beginPath();
        curve(l.source, l.target);
        ctx.setLineDash(st.dash ? st.dash.map((d) => d * lw) : []);
        ctx.strokeStyle = c ? famColor(c) : colors.ink;
        ctx.globalAlpha = 0.85;
        ctx.lineWidth = 1.6 * lw;
        ctx.stroke();
      }
    }
    ctx.setLineDash([]);

    // path trace
    let pathHead = null;
    if (path) {
      const t = now - path.start;
      const hops = path.ids.length - 1;
      ctx.lineCap = 'round';
      for (let i = 0; i < hops; i++) {
        const a = simNodes.get(path.ids[i]);
        const b = simNodes.get(path.ids[i + 1]);
        if (!a || !b) continue;
        const p = Math.max(0, Math.min(1, (t - i * HOP_MS) / HOP_MS));
        if (p <= 0) continue;
        const e = d3.easeCubicInOut(p);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(a.x + (b.x - a.x) * e, a.y + (b.y - a.y) * e);
        ctx.strokeStyle = colors.accent;
        ctx.globalAlpha = 0.95;
        ctx.lineWidth = 3 * lw;
        ctx.stroke();
        if (p < 1) pathHead = [a.x + (b.x - a.x) * e, a.y + (b.y - a.y) * e];
      }
      if (t < hops * HOP_MS + 200) animating = true;
      path.reached = Math.floor(t / HOP_MS);
    }

    // nodes
    for (const n of nodes) {
      const alpha = n.em * n.fade;
      ctx.globalAlpha = alpha;
      const r = n.r * (0.6 + 0.4 * n.fade);
      const isEvent = n.data.type === 'event';
      const onPath = pathSet?.has(n.id);
      const img = !isEvent && transform.k * r > 13 && alpha > 0.5 ? thumb(n) : null;
      if (isEvent) {
        ctx.beginPath();
        ctx.moveTo(n.x, n.y - r * 1.15);
        ctx.lineTo(n.x + r * 1.15, n.y);
        ctx.lineTo(n.x, n.y + r * 1.15);
        ctx.lineTo(n.x - r * 1.15, n.y);
        ctx.closePath();
        ctx.fillStyle = colors.paper;
        ctx.fill();
        ctx.lineWidth = 1.6 * lw;
        ctx.strokeStyle = colors.event;
        ctx.stroke();
      } else if (img) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.clip();
        const s = Math.max((r * 2) / img.width, (r * 2) / img.height);
        ctx.drawImage(img, n.x - (img.width * s) / 2, n.y - r - Math.max(0, img.height * s - r * 2) * 0.15, img.width * s, img.height * s);
        ctx.restore();
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.lineWidth = 2.2 * lw;
        ctx.strokeStyle = famColor(n);
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = famColor(n);
        ctx.fill();
        ctx.lineWidth = 1.2 * lw;
        ctx.strokeStyle = colors.paper;
        ctx.stroke();
      }
      if (n.id === focusId || onPath || n.id === hoverId) {
        const pulse = n.id === focusId ? 1 + 0.08 * Math.sin(now / 380) : 1;
        ctx.beginPath();
        ctx.arc(n.x, n.y, (r + 4 * lw * 1.6) * pulse, 0, Math.PI * 2);
        ctx.lineWidth = 1.4 * lw;
        ctx.strokeStyle = onPath ? colors.accent : colors.ink;
        ctx.globalAlpha = 0.9;
        ctx.stroke();
        if (n.id === focusId) animating = true;
      }
    }
    if (pathHead) {
      const glow = ctx.createRadialGradient(pathHead[0], pathHead[1], 0, pathHead[0], pathHead[1], 14 * lw);
      glow.addColorStop(0, colors.accent);
      glow.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.globalAlpha = 1;
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(pathHead[0], pathHead[1], 14 * lw, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
    ctx.globalAlpha = 1;
    drawLabels(strong, active, center, pathSet);
    return animating;
  }

  function curve(a, b) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const bend = 0.12;
    const s = a.id < b.id ? 1 : -1;
    ctx.moveTo(a.x, a.y);
    ctx.quadraticCurveTo((a.x + b.x) / 2 - dy * bend * s, (a.y + b.y) / 2 + dx * bend * s, b.x, b.y);
  }

  function measure(font, text) {
    const key = `${font}|${text}`;
    let w = textWidth.get(key);
    if (w == null) {
      ctx.font = font;
      w = ctx.measureText(text).width;
      textWidth.set(key, w);
    }
    return w;
  }

  function drawLabels(strong, active, center, pathSet) {
    const k = transform.k;
    const cands = [];
    for (const n of nodes) {
      const [sx, sy] = transform.apply([n.x, n.y]);
      if (sx < -80 || sy < -30 || sx > width + 80 || sy > height + 30) continue;
      const deg = index.degree.get(n.id) || 0;
      let pri = deg * k;
      let must = false;
      if (pathSet?.has(n.id)) { pri = 1e6; must = true; }
      else if (n.id === center || n.id === hoverId) { pri = 1e6 + 1; must = true; }
      else if (active && strong.has(n.id)) pri = 1e4 + deg;
      else if (active) continue;
      else if (hubRank.get(n.id) < 14) pri = 5e3 - hubRank.get(n.id);
      else if (n.r * k < 5.5 && deg * k < 14) continue;
      cands.push({ n, sx, sy, pri, must });
    }
    cands.sort((a, b) => b.pri - a.pri);
    const placed = [];
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    for (const c of cands.slice(0, 160)) {
      const big = c.must || c.pri > 40;
      const font = `${big ? 600 : 500} ${big ? 16 : 14}px "Cormorant Garamond", Georgia, serif`;
      const w = measure(font, c.n.data.title);
      const r = c.n.r * k;
      const x = c.sx - w / 2;
      const y = c.sy + r + 10;
      const box = [x - 3, y - 9, x + w + 3, y + 9];
      if (!c.must && placed.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
      placed.push(box);
      ctx.font = font;
      ctx.globalAlpha = (c.n.em ?? 1) < 0.5 ? 0.35 : c.n.fade ?? 1;
      ctx.lineWidth = 4;
      ctx.strokeStyle = colors.paper;
      ctx.strokeText(c.n.data.title, x, y);
      ctx.fillStyle = colors.ink;
      ctx.fillText(c.n.data.title, x, y);
    }
    ctx.globalAlpha = 1;
  }

  // Adaptive resolution: when consecutive drawn frames average over ~24ms the
  // bottleneck is pixel fill, so step the canvas resolution down (2 → 1.5 → 1).
  function adapt(now, drew) {
    if (drew && prevFrameAt) slowFrames.push(now - prevFrameAt);
    prevFrameAt = drew ? now : 0;
    if (slowFrames.length < 45) return;
    const avg = slowFrames.reduce((a, b) => a + b, 0) / slowFrames.length;
    slowFrames = [];
    if (avg > 24 && dpr > 1) {
      dprCap = Math.max(1, dpr - 0.5);
      resize();
    }
  }

  function frame(now) {
    const t0 = performance.now();
    let animating = false;
    const drew = dirty || path || focusId;
    adapt(now, drew);
    if (drew) {
      dirty = false;
      animating = draw(now);
      lastFrameTimes.push(performance.now() - t0);
      if (lastFrameTimes.length > 120) lastFrameTimes.shift();
    }
    if (animating) dirty = true;
    requestAnimationFrame(frame);
  }

  new ResizeObserver(() => resize()).observe(canvas);
  resize();
  requestAnimationFrame(frame);

  return {
    showCore(originId) { setVisible(coreSet(), originId); },
    showAll() { setVisible(index.nodes.map((n) => n.id)); },
    isShowingAll: () => baseVisible.size === index.nodes.length,
    visibleIds: () => nodes.map((n) => n.id),
    reveal(ids, originId) {
      const next = new Set(baseVisible);
      let changed = false;
      for (const id of ids) if (!next.has(id)) { next.add(id); changed = true; }
      if (changed) setVisible(next, originId);
      return changed;
    },
    expand(id) {
      const nb = (index.adj.get(id) || []).map((x) => x.id);
      return this.reveal([id, ...nb], id);
    },
    setFocus(id) { focusId = id; dirty = true; },
    setPath(ids) { path = ids ? { ids, start: performance.now() } : null; dirty = true; },
    pathDuration: (ids) => (ids.length - 1) * HOP_MS,
    setHiddenFamilies(set) { hiddenFamilies = new Set(set); counts = new Map(); applyVisible(); },
    setInsetRight(px) { insetRight = px; },
    setInsetLeft(px) { insetLeft = px; },
    setInsetBottom(px) { insetBottom = px; },
    flyTo, fitTo, fitAll,
    isSettled: () => sim.alpha() < 0.05,
    onStage: (id) => !!simNodes.get(id)?.onStage,
    refreshTheme() { colors = readColors(); dirty = true; },
    stats() {
      const sorted = [...lastFrameTimes].sort((a, b) => a - b);
      return { dpr, nodes: nodes.length, links: links.length, frameMsP50: sorted[Math.floor(sorted.length / 2)] ?? null, frameMsP95: sorted[Math.floor(sorted.length * 0.95)] ?? null, k: transform.k };
    },
    screenPos(id) {
      const n = simNodes.get(id);
      return n ? transform.apply([n.x, n.y]) : null;
    },
  };
}
