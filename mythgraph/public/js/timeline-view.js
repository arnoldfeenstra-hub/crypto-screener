/* global d3 */
import { FAMILIES, familyOf } from './model.js';

const M = { top: 34, right: 150, bottom: 40, left: 112 };

export function createTimelineView({ svgEl, scrubEl, labelEl, playEl, index, onHover, onSelect }) {
  const svg = d3.select(svgEl);
  const placed = index.nodes.filter((n) => n.generation != null);
  const unplaced = index.nodes.filter((n) => n.generation == null);
  const maxGen = d3.max(placed, (n) => n.generation) ?? 1;
  const lanes = Object.keys(FAMILIES);
  const r = (n) => Math.min(9, 2.6 + Math.sqrt(index.degree.get(n.id) || 0) * 0.95);

  let width = 0;
  let height = 0;
  let x0 = d3.scaleLinear();
  let x = x0;
  const y = d3.scaleBand().domain(lanes).paddingInner(0.12);
  let now = maxGen;
  let selected = null;
  let hovered = null;
  let playing = null;
  let pos = new Map(); // id -> {gx (swarm x at base scale), y}

  scrubEl.min = 0;
  scrubEl.max = String(Math.ceil(maxGen * 10) / 10);
  scrubEl.value = scrubEl.max;

  const gGrid = svg.append('g').attr('class', 'tl-grid');
  const gAxis = svg.append('g').attr('class', 'tl-axis');
  const gLanes = svg.append('g');
  const gThreads = svg.append('g');
  const gDots = svg.append('g');
  const gLabels = svg.append('g');
  const nowLine = svg.append('line').attr('class', 'tl-now');
  const nowLabel = svg.append('text').attr('class', 'tl-now-label').attr('text-anchor', 'middle');
  const unplacedLabel = svg.append('text').attr('class', 'tl-unplaced-label');

  const colorVar = (n) => `var(--${familyOf(n)})`;

  const zoom = d3
    .zoom()
    .scaleExtent([1, 10])
    .filter((ev) => !ev.button && ev.type !== 'dblclick')
    .on('zoom', (ev) => {
      x = ev.transform.rescaleX(x0);
      render(false);
    });
  svg.call(zoom);

  function layout() {
    const rect = svgEl.getBoundingClientRect();
    width = rect.width;
    height = rect.height;
    if (!width || !height) return;
    const narrow = width < 640;
    M.left = narrow ? 70 : 112;
    M.right = unplaced.length ? (narrow ? 70 : 150) : 24;
    x0 = d3.scaleLinear().domain([-0.3, maxGen + 0.3]).range([M.left, width - M.right]);
    x = d3.zoomTransform(svgEl).rescaleX(x0);
    y.range([M.top, height - M.bottom]);
    zoom.translateExtent([[M.left, 0], [width - M.right, height]]).extent([[M.left, 0], [width - M.right, height]]);
    swarm();
    render(true);
  }

  // Beeswarm at the base scale; zooming shifts dots by their offset from the
  // generation position instead of re-solving on every wheel event.
  function swarm() {
    const items = placed.map((n) => ({ id: n.id, n, r: r(n), tx: x0(n.generation), ty: y(familyOf(n)) + y.bandwidth() / 2 }));
    const sim = d3
      .forceSimulation(items)
      .force('x', d3.forceX((d) => d.tx).strength(0.9))
      .force('y', d3.forceY((d) => d.ty).strength(0.06))
      .force('collide', d3.forceCollide((d) => d.r + 1.2).iterations(2))
      .stop();
    for (const d of items) { d.x = d.tx; d.y = d.ty + (Math.random() - 0.5) * 10; }
    for (let i = 0; i < 180; i++) sim.tick();
    pos = new Map();
    for (const d of items) {
      const band = y(familyOf(d.n));
      pos.set(d.id, { off: d.x - d.tx, y: Math.max(band + d.r, Math.min(band + y.bandwidth() - d.r, d.y)) });
    }
    // unplaced: a compact grid per lane in the right-hand margin
    const byLane = d3.group(unplaced, (n) => familyOf(n));
    for (const [lane, list] of byLane) {
      const band = y(lane);
      const cols = Math.max(2, Math.floor((M.right - 34) / 13));
      list.forEach((n, i) => {
        pos.set(n.id, {
          fixedX: width - M.right + 22 + (i % cols) * 13,
          y: band + 10 + Math.floor(i / cols) * 13,
        });
      });
    }
  }

  const px = (n) => {
    const p = pos.get(n.id);
    return p.fixedX ?? x(n.generation) + p.off;
  };

  function render(full) {
    if (!width) return;
    // grid + axis
    const ticks = x.ticks(Math.min(12, Math.max(4, Math.round(width / 110)))).filter((t) => Number.isInteger(t) && t >= 0 && t <= maxGen);
    gGrid.selectAll('line').data(ticks).join('line')
      .attr('x1', (t) => x(t)).attr('x2', (t) => x(t)).attr('y1', M.top - 8).attr('y2', height - M.bottom);
    gAxis.attr('transform', `translate(0,${height - M.bottom})`)
      .call(d3.axisBottom(x).tickValues(ticks).tickFormat((t) => (t === 0 ? 'Gen 0' : `${t}`)).tickSizeOuter(0))
      .call((g) => g.select('.domain').attr('d', `M${M.left},0H${width - M.right}`));
    gAxis.selectAll('.axis-title').data([0]).join('text').attr('class', 'axis-title')
      .attr('x', width - M.right).attr('y', 30).attr('text-anchor', 'end')
      .text('generations after the first beings →');

    if (full) {
      gLanes.selectAll('text').data(lanes).join('text').attr('class', 'tl-lane-label')
        .attr('x', 20).attr('y', (l) => y(l) + y.bandwidth() / 2).attr('dy', '0.35em').text((l) => FAMILIES[l].label);
      gLanes.selectAll('line').data(lanes.slice(1)).join('line')
        .attr('x1', 16).attr('x2', width - 16).attr('y1', (l) => y(l) - (y.step() - y.bandwidth()) / 2).attr('y2', (l) => y(l) - (y.step() - y.bandwidth()) / 2)
        .attr('stroke', 'var(--rule-soft)');
      unplacedLabel.attr('x', width - M.right + 22).attr('y', M.top - 14)
        .text(unplaced.length ? `Unplaced · ${unplaced.length}` : '');
    }

    const nb = selected ? new Set((index.adj.get(selected) || []).map((a) => a.id)) : null;
    const dots = gDots.selectAll('.tl-dot').data(index.nodes, (d) => d.id).join((enter) =>
      enter.append('path').attr('class', 'tl-dot')
        .on('pointerenter', (ev, d) => { hovered = d.id; const [mx, my] = d3.pointer(ev, svgEl); onHover(d.id, mx, my); styleDots(); })
        .on('pointerleave', () => { hovered = null; onHover(null); styleDots(); })
        .on('click', (ev, d) => { ev.stopPropagation(); onSelect(d.id); }),
    );
    dots.attr('d', (d) => glyph(d, px(d), pos.get(d.id).y, r(d)))
      .attr('fill', (d) => (d.type === 'event' ? 'var(--paper)' : colorVar(d)))
      .attr('stroke', (d) => (d.type === 'event' ? 'var(--event)' : 'var(--paper)'))
      .attr('stroke-width', (d) => (d.type === 'event' ? 1.4 : 0.8));
    styleDots(nb);
    drawThreads(nb);
    drawNow();
  }

  function glyph(d, cx, cy, rr) {
    if (d.type === 'event') {
      const s = rr * 1.2;
      return `M${cx},${cy - s}L${cx + s},${cy}L${cx},${cy + s}L${cx - s},${cy}Z`;
    }
    return `M${cx - rr},${cy}a${rr},${rr} 0 1,0 ${rr * 2},0a${rr},${rr} 0 1,0 ${-rr * 2},0`;
  }

  function isPast(d) {
    return d.generation == null || d.generation <= now + 0.05;
  }

  function styleDots(nb = selected ? new Set((index.adj.get(selected) || []).map((a) => a.id)) : null) {
    gDots.selectAll('.tl-dot').attr('opacity', (d) => {
      if (selected) return d.id === selected || nb.has(d.id) ? 1 : 0.1;
      if (d.id === hovered) return 1;
      return isPast(d) ? (Math.abs((d.generation ?? -99) - now) < 0.55 ? 1 : 0.62) : 0.08;
    });
    drawLabels(nb);
  }

  function drawThreads(nb) {
    const data = selected ? [...nb].map((id) => index.byId.get(id)) : [];
    const s = selected ? index.byId.get(selected) : null;
    gThreads.selectAll('path').data(data, (d) => d.id).join('path').attr('class', 'tl-thread')
      .attr('stroke', s ? colorVar(s) : 'none')
      .attr('d', (d) => {
        const ax = px(s), ay = pos.get(s.id).y, bx = px(d), by = pos.get(d.id).y;
        const mx = (ax + bx) / 2, my = Math.min(ay, by) - Math.abs(bx - ax) * 0.18 - 12;
        return `M${ax},${ay}Q${mx},${my} ${bx},${by}`;
      });
  }

  function drawLabels(nb) {
    let cands;
    if (selected) cands = [selected, ...nb].map((id) => index.byId.get(id));
    else {
      cands = placed.filter((d) => Math.abs(d.generation - now) < 0.55 && d.generation <= now + 0.05)
        .sort((a, b) => index.degree.get(b.id) - index.degree.get(a.id));
    }
    if (hovered && !cands.some((d) => d.id === hovered)) cands.unshift(index.byId.get(hovered));
    const boxes = [];
    const shown = [];
    for (const d of cands) {
      const w = d.title.length * 6.6;
      const cx = px(d);
      const cy = pos.get(d.id).y - r(d) - 7;
      const b = [cx - w / 2, cy - 11, cx + w / 2, cy + 3];
      if (d.id !== selected && d.id !== hovered && boxes.some((o) => b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1])) continue;
      boxes.push(b);
      shown.push({ d, cx, cy });
      if (shown.length > (selected ? 60 : 18)) break;
    }
    gLabels.selectAll('text').data(shown, (s) => s.d.id).join('text').attr('class', 'tl-label')
      .attr('text-anchor', 'middle').attr('x', (s) => s.cx).attr('y', (s) => s.cy)
      .style('font-weight', (s) => (s.d.id === selected ? 600 : null))
      .text((s) => s.d.title);
  }

  function drawNow() {
    const nx = x(now);
    const visible = nx >= M.left - 1 && nx <= width - M.right + 1;
    nowLine.attr('x1', nx).attr('x2', nx).attr('y1', M.top - 10).attr('y2', height - M.bottom).attr('opacity', visible ? 1 : 0);
    nowLabel.attr('x', nx).attr('y', M.top - 16).attr('opacity', visible ? 1 : 0).text(`generation ${now.toFixed(1)}`);
    labelEl.textContent = `Generation ${now.toFixed(1)} of ${maxGen.toFixed(1)}`;
  }

  function setNow(v) {
    now = Math.max(0, Math.min(maxGen, v));
    scrubEl.value = String(now);
    styleDots();
    drawNow();
  }

  scrubEl.addEventListener('input', () => {
    stop();
    setNow(Number(scrubEl.value));
  });
  svg.on('click', () => onSelect(null));

  function stop() {
    if (playing) { playing.stop(); playing = null; playEl.setAttribute('aria-label', 'Play timeline'); }
  }
  playEl.addEventListener('click', () => {
    if (playing) return stop();
    const start = now >= maxGen - 0.01 ? 0 : now;
    const dur = 14000 * ((maxGen - start) / maxGen);
    playEl.setAttribute('aria-label', 'Pause timeline');
    playing = d3.timer((t) => {
      setNow(start + (maxGen - start) * Math.min(1, t / dur));
      if (t >= dur) stop();
    });
  });

  new ResizeObserver(() => layout()).observe(svgEl);

  return {
    layout,
    select(id) {
      selected = id;
      render(false);
      if (id) {
        const d = index.byId.get(id);
        if (d.generation != null) {
          const t = d3.zoomTransform(svgEl);
          const sx = t.rescaleX(x0)(d.generation);
          if (sx < M.left || sx > width - M.right) svg.transition().duration(700).call(zoom.translateBy, ((width - M.left - M.right) / 2 + M.left - sx) / t.k, 0);
        }
      }
    },
    setNow,
    stats: () => ({ placed: placed.length, unplaced: unplaced.length, maxGen, now }),
  };
}
