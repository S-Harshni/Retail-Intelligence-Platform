'use strict';

// ---------- small DOM helpers (all text goes through textContent / append, never innerHTML) ----------
const NS = 'http://www.w3.org/2000/svg';

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null || v === '') continue;
    if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  e.append(...kids.flat().filter(k => k != null && k !== ''));
  return e;
}

function s(tag, attrs, parent, text) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  if (parent) parent.append(e);
  return e;
}

// ---------- formatting ----------
const nf = new Intl.NumberFormat('en-GB');
const compact = v => {
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e8 ? 0 : a >= 1e7 ? 1 : 2) + 'M';
  if (a >= 1e4) return (v / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'K';
  return nf.format(Math.round(v));
};
const day = d => new Date(d + 'T00:00:00Z');
const F = {
  int: v => nf.format(Math.round(v)),
  gbp: v => '£' + compact(v),
  gbpFull: v => '£' + nf.format(Math.round(v)),
  pct: (v, d = 1) => (v * 100).toFixed(d) + '%',
  pct0: v => Math.round(v * 100) + '%',
  dec: (v, d = 2) => v.toFixed(d),
  month: d => day(d).toLocaleDateString('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' }),
  monthShort: d => day(d).toLocaleDateString('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' }),
  week: d => day(d).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }),
};
const C = { actual: 'var(--series-1)', model: 'var(--series-2)', baseline: 'var(--series-3)' };

function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}
function ticksFor(min, max, n = 4) {
  const step = niceStep((max - min) / n || 1);
  const out = [];
  for (let v = Math.floor(min / step) * step; v < max + step * 0.999; v += step) out.push(+v.toPrecision(12));
  return out;
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// ---------- one tooltip for every chart ----------
const tip = h('div', { class: 'tip', hidden: true });
document.body.append(tip);
function showTip(cx, cy, title, rows) {
  tip.replaceChildren(
    h('div', { class: 'tip-title', text: title }),
    ...rows.filter(Boolean).map(r => h('div', { class: 'tip-row' },
      r.color ? h('i', { style: `background:${r.color}` }) : null, h('strong', { text: r.value }), h('span', { text: r.name }))));
  tip.hidden = false;
  const w = tip.offsetWidth, hh = tip.offsetHeight;
  tip.style.left = clamp(cx + 14, 8, innerWidth - w - 8) + 'px';
  tip.style.top = (cy + 16 + hh > innerHeight ? cy - hh - 12 : cy + 16) + 'px';
}
const hideTip = () => { tip.hidden = true; };

// ---------- building blocks ----------
const renders = [];
const renderAll = () => renders.forEach(r => r());

function dataTable(columns, rows, best) {
  return h('table', {},
    h('thead', {}, h('tr', {}, columns.map(c => h('th', { class: c.num ? 'num' : '', text: c.label })))),
    h('tbody', {}, rows.map(r => h('tr', { class: best && best(r) ? 'best' : '' }, columns.map(c => {
      const v = r[c.key];
      const out = c.render ? c.render(v, r) : v == null ? '–' : c.format ? c.format(v) : v;
      return h('td', { class: c.num ? 'num' : '' }, out instanceof Node ? out : String(out));
    })))));
}

function tiles(items) {
  return h('div', { class: 'tiles' }, items.map(([label, value, note]) => h('div', { class: 'tile' },
    h('div', { class: 'label', text: label }), h('div', { class: 'value', text: value }), note ? h('div', { class: 'note', text: note }) : null)));
}

// A chart card: title, optional legend, the plot, and a table-view twin of the same data.
function card({ title, sub, legend, table, wide, notes }, draw) {
  const plot = h('div', { class: 'plot' });
  const tableBox = h('div', { class: 'table-box', hidden: true });
  const legendEl = legend ? h('div', { class: 'legend' }, legend.map(l => h('span', {}, h('i', { class: l.shape, style: `background:${l.color}` }), l.name))) : null;
  const paint = () => { if (draw && plot.isConnected && !plot.hidden && plot.clientWidth) draw(plot); };
  const btn = table && draw ? h('button', {
    class: 'ghost', type: 'button', text: 'Table', 'aria-pressed': 'false',
    onclick: () => {
      const on = tableBox.hidden;
      tableBox.replaceChildren(dataTable(table.columns, typeof table.rows === 'function' ? table.rows() : table.rows));
      tableBox.hidden = !on; plot.hidden = on;
      if (legendEl) legendEl.hidden = on;
      btn.textContent = on ? 'Chart' : 'Table';
      btn.setAttribute('aria-pressed', String(on));
      paint();
    },
  }) : null;
  if (table && !draw) { tableBox.hidden = false; tableBox.append(dataTable(table.columns, table.rows, table.best)); }
  renders.push(paint);
  const fig = h('figure', { class: 'card' + (wide ? ' wide' : '') },
    h('div', { class: 'card-head' }, h('div', {}, h('h3', { text: title }), sub ? h('p', { class: 'sub', text: sub }) : null), btn),
    legendEl, plot, tableBox, notes ? h('ul', { class: 'note-list' }, notes.map(n => h('li', { text: n }))) : null);
  fig.repaint = paint;
  return fig;
}

function frame(plot, height, label) {
  const W = plot.clientWidth;
  plot.replaceChildren();
  return { W, root: s('svg', { viewBox: `0 0 ${W} ${height}`, width: W, height, role: 'img', 'aria-label': label, tabindex: 0 }, plot) };
}

function yAxis(root, ticks, y, m, W, format) {
  for (const t of ticks) {
    s('line', { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: t === ticks[0] ? 'axis-line' : 'grid-line' }, root);
    s('text', { x: m.l - 8, y: y(t) + 4, class: 'tick', 'text-anchor': 'end' }, root, format(t));
  }
}

// Lines over an ordered x (dates). Crosshair snaps to the nearest x and lists every series.
function lineChart(plot, { labels, series, yFormat = F.int, xFormat = x => x, tipTitle = xFormat, height = 270, area = false, mark, label }) {
  const { W, root } = frame(plot, height, label);
  const m = { l: 54, r: 14, t: 14, b: 28 };
  const ticks = ticksFor(0, Math.max(...series.flatMap(d => d.values.filter(v => v != null))), 4);
  const last = labels.length - 1, innerW = W - m.l - m.r;
  const x = i => m.l + (last ? i * innerW / last : innerW / 2);
  const y = v => m.t + (height - m.t - m.b) * (1 - v / ticks.at(-1));
  yAxis(root, ticks, y, m, W, yFormat);
  const every = Math.ceil(labels.length / Math.max(2, Math.floor(innerW / 76)));
  for (let i = 0; i <= last; i += every) {
    s('text', { x: x(i), y: height - 8, class: 'tick', 'text-anchor': i === 0 ? 'start' : 'middle' }, root, xFormat(labels[i]));
  }
  if (mark) {
    s('line', { x1: x(mark.index), x2: x(mark.index), y1: m.t, y2: height - m.b, class: 'axis-line' }, root);
    s('text', { x: x(mark.index) + 6, y: m.t + 4, class: 'axis-title' }, root, mark.label);
  }
  for (const d of series) {
    let path = '', pen = false, first = null, final = null;
    d.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      path += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true; first ??= i; final = i;
    });
    if (area && first != null) s('path', { d: `${path}L${x(final)},${y(0)}L${x(first)},${y(0)}Z`, class: 'area', style: `fill:${d.color}` }, root);
    s('path', { d: path, class: 'line', style: `stroke:${d.color}` }, root);
  }
  const cross = s('line', { y1: m.t, y2: height - m.b, class: 'cross', visibility: 'hidden' }, root);
  const dots = series.map(d => s('circle', { r: 4.5, class: 'ring', style: `fill:${d.color}`, visibility: 'hidden' }, root));
  let at = -1;
  const show = (i, cx, cy) => {
    at = i;
    cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
    series.forEach((d, k) => {
      const v = d.values[i];
      dots[k].setAttribute('visibility', v == null ? 'hidden' : 'visible');
      if (v != null) { dots[k].setAttribute('cx', x(i)); dots[k].setAttribute('cy', y(v)); }
    });
    showTip(cx, cy, tipTitle(labels[i]), series.map(d => d.values[i] == null ? null : { color: d.color, name: d.name, value: yFormat(d.values[i]) }));
  };
  const hide = () => { cross.setAttribute('visibility', 'hidden'); dots.forEach(d => d.setAttribute('visibility', 'hidden')); hideTip(); };
  const overlay = s('rect', { x: m.l, y: m.t, width: innerW, height: height - m.t - m.b, fill: 'transparent' }, root);
  overlay.addEventListener('pointermove', e => {
    const px = e.clientX - root.getBoundingClientRect().left;
    show(clamp(Math.round((px - m.l) / (innerW / (last || 1))), 0, last), e.clientX, e.clientY);
  });
  overlay.addEventListener('pointerleave', hide);
  root.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const i = clamp((at < 0 ? last : at) + (e.key === 'ArrowRight' ? 1 : -1), 0, last);
    const box = root.getBoundingClientRect();
    show(i, box.left + x(i), box.top + m.t);
  });
  root.addEventListener('blur', hide);
}

// Columns from one baseline, thin, rounded at the data end. Optional markers for a second measure on the same scale.
function columnChart(plot, { labels, values, name, yFormat = F.int, height = 250, markers, label }) {
  const { W, root } = frame(plot, height, label);
  const m = { l: 50, r: 10, t: 18, b: 28 };
  const ticks = ticksFor(0, Math.max(...values, ...(markers ? markers.values : [])), 4);
  const band = (W - m.l - m.r) / labels.length, bw = Math.min(24, band * 0.6);
  const y = v => m.t + (height - m.t - m.b) * (1 - v / ticks.at(-1));
  yAxis(root, ticks, y, m, W, yFormat);
  labels.forEach((lab, i) => {
    const cx = m.l + band * (i + 0.5), top = y(values[i]), base = y(0), r = Math.min(4, bw / 2, base - top);
    const bar = s('path', {
      class: 'col', style: `fill:${C.actual}`,
      d: `M${cx - bw / 2},${base}V${top + r}Q${cx - bw / 2},${top} ${cx - bw / 2 + r},${top}H${cx + bw / 2 - r}Q${cx + bw / 2},${top} ${cx + bw / 2},${top + r}V${base}Z`,
    }, root);
    if (labels.length <= 7) s('text', { x: cx, y: top - 5, class: 'cap', 'text-anchor': 'middle' }, root, yFormat(values[i]));
    s('text', { x: cx, y: height - 8, class: 'tick', 'text-anchor': 'middle' }, root, lab);
    if (markers) s('circle', { cx, cy: y(markers.values[i]), r: 5, class: 'ring', style: `fill:${markers.color}` }, root);
    const hit = s('rect', { x: m.l + band * i, y: m.t, width: band, height: height - m.t - m.b, fill: 'transparent', tabindex: 0 }, root);
    const rows = [{ color: C.actual, name, value: yFormat(values[i]) }, markers ? { color: markers.color, name: markers.name, value: yFormat(markers.values[i]) } : null];
    const on = (cx2, cy2) => { bar.classList.add('on'); showTip(cx2, cy2, lab, rows); };
    hit.addEventListener('pointermove', e => on(e.clientX, e.clientY));
    hit.addEventListener('focus', () => { const b = hit.getBoundingClientRect(); on(b.left + b.width / 2, b.top + 20); });
    for (const ev of ['pointerleave', 'blur']) hit.addEventListener(ev, () => { bar.classList.remove('on'); hideTip(); });
  });
}

// Horizontal bars in HTML: long category names wrap instead of being clipped; value sits at the bar tip.
function hbars(plot, { rows, series, format, max }) {
  const top = max ?? Math.max(...rows.flatMap(r => r.values));
  plot.replaceChildren(h('div', { class: 'hbars' }, rows.map(r => {
    const row = h('div', { class: 'hrow', tabindex: 0 }, h('div', { class: 'name', text: r.label }),
      h('div', { class: 'bars' }, r.values.map((v, i) => h('div', { class: 'track' },
        h('div', { class: 'bar', style: `width:${(v / top * 80).toFixed(2)}%;background:${series[i].color}` }),
        h('span', { class: 'val', text: format(v) })))));
    const rowsTip = r.values.map((v, i) => ({ color: series[i].color, name: series[i].name, value: format(v) })).concat(r.extra || []);
    row.addEventListener('pointermove', e => showTip(e.clientX, e.clientY, r.label, rowsTip));
    row.addEventListener('focus', () => { const b = row.getBoundingClientRect(); showTip(b.left + 120, b.top + 8, r.label, rowsTip); });
    for (const ev of ['pointerleave', 'blur']) row.addEventListener(ev, hideTip);
    return row;
  })));
}

// Two numeric axes (one scale each), lines with markers. Each marker has a generous hit area.
function xyChart(plot, { series, xFormat, yFormat, xTitle, height = 320, label }) {
  const { W, root } = frame(plot, height, label);
  const m = { l: 54, r: 18, t: 14, b: 44 };
  const pts = series.flatMap(d => d.points);
  const xt = ticksFor(Math.min(...pts.map(p => p.x)), Math.max(...pts.map(p => p.x)), Math.max(3, Math.floor((W - m.l - m.r) / 120)));
  const yt = ticksFor(Math.min(...pts.map(p => p.y)), Math.max(...pts.map(p => p.y)), 4);
  const x = v => m.l + (W - m.l - m.r) * (v - xt[0]) / (xt.at(-1) - xt[0]);
  const y = v => m.t + (height - m.t - m.b) * (1 - (v - yt[0]) / (yt.at(-1) - yt[0]));
  yAxis(root, yt, y, m, W, yFormat);
  for (const t of xt) s('text', { x: x(t), y: height - m.b + 16, class: 'tick', 'text-anchor': 'middle' }, root, xFormat(t));
  s('text', { x: m.l + (W - m.l - m.r) / 2, y: height - 6, class: 'axis-title', 'text-anchor': 'middle' }, root, xTitle);
  for (const d of series) {
    s('path', { d: d.points.map((p, i) => `${i ? 'L' : 'M'}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`).join(''), class: 'line', style: `stroke:${d.color}` }, root);
  }
  for (const d of series) {
    for (const p of d.points) {
      s('circle', { cx: x(p.x), cy: y(p.y), r: 4.5, class: 'ring', style: `fill:${d.color}` }, root);
      const hit = s('circle', { cx: x(p.x), cy: y(p.y), r: 13, fill: 'transparent', tabindex: 0 }, root);
      hit.addEventListener('pointermove', e => showTip(e.clientX, e.clientY, d.name, p.tip));
      hit.addEventListener('focus', () => { const b = hit.getBoundingClientRect(); showTip(b.left + 13, b.top + 13, d.name, p.tip); });
      for (const ev of ['pointerleave', 'blur']) hit.addEventListener(ev, hideTip);
    }
  }
}

const section = (...kids) => h('section', {}, kids);
const intro = text => h('p', { class: 'intro', text });
const grid = (...kids) => h('div', { class: 'grid' }, kids);

// ---------- tabs ----------
const TABS = {
  Overview(D) {
    const o = D.overview, t = o.totals, c = D.customers.summary;
    const abroad = o.countries.filter(x => x.country !== 'United Kingdom').slice(0, 8);
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const sales = Object.fromEntries(o.weekdays.map(w => [w.weekday, w.revenue]));
    return section(
      intro('Two years of orders from a UK online gift-ware retailer whose customers are mostly wholesalers. Every figure on this site is computed from the published transactions by the SQL and Python in the repository.'),
      tiles([
        ['Gross sales', F.gbp(t.gross_sales), `${F.month(o.monthly[0].month)} to ${F.month(o.monthly.at(-1).month)}`],
        ['Orders', F.int(t.orders), `average order ${F.gbpFull(t.average_order_value)}`],
        ['Customers with an id', F.int(c.customers), `${F.pct0(1 - D.quality.revenue_share_without_customer)} of sales value carries an id`],
        ['Products sold', F.int(t.products), `${t.countries} countries, ${F.pct0(t.export_share)} of sales outside the UK`],
      ]),
      grid(
        card({
          title: 'Monthly sales', sub: 'Gross sales by order month. The last month covers nine days.', wide: true,
          table: { columns: [{ label: 'Month', key: 'month', format: F.month }, { label: 'Sales', key: 'revenue', num: true, format: F.gbpFull }, { label: 'Orders', key: 'orders', num: true, format: F.int }, { label: 'Customers', key: 'customers', num: true, format: F.int }], rows: o.monthly },
        }, p => lineChart(p, { labels: o.monthly.map(x => x.month), series: [{ name: 'Sales', color: C.actual, values: o.monthly.map(x => x.revenue) }], yFormat: F.gbp, xFormat: F.monthShort, tipTitle: F.month, area: true, label: 'Monthly sales' })),
        card({
          title: 'Sales outside the UK', sub: `The UK is ${F.pct(o.countries[0].share)} of sales. These are the next eight markets.`,
          table: { columns: [{ label: 'Country', key: 'country' }, { label: 'Sales', key: 'revenue', num: true, format: F.gbpFull }, { label: 'Orders', key: 'orders', num: true, format: F.int }, { label: 'Customers', key: 'customers', num: true, format: F.int }], rows: o.countries },
        }, p => hbars(p, { rows: abroad.map(x => ({ label: x.country, values: [x.revenue], extra: [{ name: 'orders', value: F.int(x.orders) }] })), series: [{ name: 'Sales', color: C.actual }], format: F.gbp })),
        card({
          title: 'Sales by day of the week', sub: 'Almost nothing is ordered on Saturdays.',
          table: { columns: [{ label: 'Day', key: 'day' }, { label: 'Sales', key: 'revenue', num: true, format: F.gbpFull }, { label: 'Orders', key: 'orders', num: true, format: F.int }], rows: o.weekdays.map(w => ({ ...w, day: days[w.weekday - 1] })) },
        }, p => columnChart(p, { labels: days, values: days.map((_, i) => sales[i + 1] || 0), name: 'Sales', yFormat: F.gbp, label: 'Sales by weekday' })),
        card({
          title: 'Best-selling products', sub: 'By gross sales, leaving out products whose volume is mostly cancelled orders.', wide: true,
          table: { columns: [{ label: 'Product', key: 'description' }, { label: 'Code', key: 'stock_code' }, { label: 'Sales', key: 'revenue', num: true, format: F.gbpFull }, { label: 'Units', key: 'units_sold', num: true, format: F.int }, { label: 'Orders', key: 'orders', num: true, format: F.int }], rows: o.top_products },
        })));
  },

  Customers(D) {
    const c = D.customers, m = c.model, sm = c.summary;
    const best = m.models.find(x => x.model === m.best_model), recency = m.models[0];
    const months = [...Array(12)].map((_, i) => i + 1);
    const heat = h('table', { class: 'heat' },
      h('thead', {}, h('tr', {}, h('th', { class: 'row', text: 'First order' }), h('th', { class: 'num', text: 'Customers' }), months.map(k => h('th', { text: `+${k}` })))),
      h('tbody', {}, c.cohorts.map(co => h('tr', {}, h('th', { class: 'row', text: F.month(co.cohort + '-01') }), h('td', { class: 'num', text: F.int(co.size) }),
        months.map(k => {
          const v = co.retention[k];
          if (v == null) return h('td', {});
          const cell = h('td', { class: 's' + clamp(Math.floor(v / 0.07), 0, 6), text: F.pct0(v) });
          cell.addEventListener('pointermove', e => showTip(e.clientX, e.clientY, `First order in ${F.month(co.cohort + '-01')}`, [{ value: F.pct(v), name: `of ${F.int(co.size)} customers ordered again ${k} month${k > 1 ? 's' : ''} later` }]));
          cell.addEventListener('pointerleave', hideTip);
          return cell;
        })))));
    const cohortCard = h('figure', { class: 'card wide' },
      h('div', { class: 'card-head' }, h('div', {}, h('h3', { text: 'Do new customers come back?' }), h('p', { class: 'sub', text: 'Share of each month\'s first-time customers who order again 1 to 12 months later. The first row holds every customer already active when the data starts, so it is not a true new-customer group.' }))),
      h('div', { class: 'table-box' }, heat),
      h('div', { class: 'scale' }, h('span', { text: '0%' }), [...Array(7)].map((_, i) => h('i', { style: `background:var(--seq-${i})` })), h('span', { text: '42% or more' })));
    return section(
      intro(`${F.int(sm.customers)} customers can be followed through an id. A small core does most of the buying, so the useful questions are who that core is, whether new customers return, and who is likely to order next.`),
      tiles([
        ['Ordered more than once', F.pct0(sm.repeat_rate), `median customer: ${sm.median_orders} orders, ${F.gbpFull(sm.median_value)}`],
        ['Sales from the top 20% of customers', F.pct0(sm.top20_revenue_share), 'by net spend'],
        ['New customers who order again next month', F.pct0(sm.month1_retention), `${F.pct0(sm.month3_retention)} three months later`],
        ['Repeat-order model, ROC AUC', F.dec(best.roc_auc, 2), `${F.dec(recency.roc_auc, 2)} for ranking by recency alone`],
      ]),
      grid(
        card({
          title: 'Customer segments', sub: 'Recency, frequency and monetary value, each scored 1 to 5 by quintile, then grouped.', wide: true,
          legend: [{ name: 'Share of customers', color: C.actual, shape: 'box' }, { name: 'Share of sales', color: C.model, shape: 'box' }],
          table: { columns: [{ label: 'Segment', key: 'segment' }, { label: 'Customers', key: 'customers', num: true, format: F.int }, { label: 'Share of customers', key: 'customer_share', num: true, format: F.pct }, { label: 'Net sales', key: 'revenue', num: true, format: F.gbpFull }, { label: 'Share of sales', key: 'revenue_share', num: true, format: F.pct }, { label: 'Avg days since last order', key: 'recency_days', num: true, format: F.int }, { label: 'Avg orders', key: 'orders', num: true, format: v => F.dec(v, 1) }], rows: c.segments },
        }, p => hbars(p, { rows: c.segments.map(x => ({ label: x.segment, values: [x.customer_share, x.revenue_share], extra: [{ name: 'customers', value: F.int(x.customers) }, { name: 'average orders', value: F.dec(x.orders, 1) }] })), series: [{ name: 'of customers', color: C.actual }, { name: 'of sales', color: C.model }], format: F.pct, max: 1 })),
        cohortCard,
        card({
          title: 'Who will order in the next 90 days?', sub: `Customers ranked by the model's score and cut into ten equal groups. Tested on the ${m.horizon_days} days after ${F.week(m.test_cutoff)}, which the model never saw.`,
          legend: [{ name: 'Actually ordered', color: C.actual, shape: 'box' }, { name: 'Predicted', color: C.model, shape: 'dot' }],
          table: { columns: [{ label: 'Group (1 = highest score)', key: 'decile' }, { label: 'Customers', key: 'customers', num: true, format: F.int }, { label: 'Predicted', key: 'predicted', num: true, format: F.pct }, { label: 'Actually ordered', key: 'actual', num: true, format: F.pct }], rows: m.deciles },
          notes: [`The ranking works: ${F.pct0(m.deciles[0].actual)} of the top group ordered, against ${F.pct0(m.test_repeat_rate)} of all customers.`, 'The predicted rates run low because the test window is the pre-Christmas peak and the model has only seen one earlier autumn. Use the ranking, not the raw probability.'],
        }, p => columnChart(p, { labels: m.deciles.map(d => String(d.decile)), values: m.deciles.map(d => d.actual), name: 'actually ordered', yFormat: F.pct0, markers: { name: 'predicted', color: C.model, values: m.deciles.map(d => d.predicted) }, label: 'Repeat rate by score group' })),
        card({
          title: 'What the model looks at', sub: 'Drop in ROC AUC on the test set when one input is shuffled.',
          table: { columns: [{ label: 'Input', key: 'label' }, { label: 'AUC drop', key: 'auc_drop', num: true, format: v => F.dec(v, 4) }], rows: m.importance },
        }, p => hbars(p, { rows: m.importance.map(x => ({ label: x.label, values: [Math.max(x.auc_drop, 0)] })), series: [{ name: 'AUC drop', color: C.actual }], format: v => F.dec(v, 3) })),
        card({
          title: 'Model comparison on the test window', wide: true,
          sub: `Trained on ${F.int(m.train_rows)} customer snapshots from ${m.train_cutoffs.length} earlier cut-off dates; tested on ${F.int(m.test_customers)} customers.`,
          table: { columns: [{ label: 'Method', key: 'model' }, { label: 'ROC AUC', key: 'roc_auc', num: true, format: v => F.dec(v, 3) }, { label: 'Average precision', key: 'average_precision', num: true, format: v => F.dec(v, 3) }, { label: 'Top 10% who ordered', key: 'top_decile_rate', num: true, format: F.pct }, { label: 'Lift over average', key: 'top_decile_lift', num: true, format: v => F.dec(v, 2) + '×' }], rows: m.models, best: r => r.model === m.best_model },
        })));
  },

  Returns(D) {
    const r = D.returns, sm = r.summary;
    const peak = r.monthly.slice().sort((a, b) => b.value_rate - a.value_rate).slice(0, 2).map(x => F.month(x.month));
    return section(
      intro('Returns are cancellation invoices in the source. A sale line counts as fully reversed when the same customer later returns the same product in the same quantity.'),
      tiles([
        ['Sales value returned', F.pct(sm.value_rate), `${F.gbp(D.overview.totals.returned_value)} of ${F.gbp(D.overview.totals.gross_sales)}`],
        ['Return lines', F.int(sm.return_lines), `${F.pct(sm.line_rate)} of sale lines`],
        ['Orders with a return', F.int(sm.return_orders), 'cancellation invoices'],
        ['Returns that undo a whole line', F.pct0(sm.full_reversal_share), `${F.int(sm.reversed_lines)} sale lines fully reversed`],
      ]),
      grid(
        card({
          title: 'Share of sales value returned, by month', wide: true,
          sub: `The peaks in ${peak[1]} and ${peak[0]} are two bulk orders of 74,215 and 80,995 units, each cancelled within minutes.`,
          table: { columns: [{ label: 'Month', key: 'month', format: F.month }, { label: 'Returned value', key: 'returned_value', num: true, format: F.gbpFull }, { label: 'Share of sales', key: 'value_rate', num: true, format: F.pct }], rows: r.monthly },
        }, p => lineChart(p, { labels: r.monthly.map(x => x.month), series: [{ name: 'Returned', color: C.actual, values: r.monthly.map(x => x.value_rate) }], yFormat: F.pct0, xFormat: F.monthShort, tipTitle: F.month, label: 'Monthly return rate' })),
        card({
          title: 'Products sent back most often', sub: 'Return lines per sale line, for products with at least 300 sale lines. Fragile items lead.',
          table: { columns: [{ label: 'Product', key: 'description' }, { label: 'Sale lines', key: 'sale_lines', num: true, format: F.int }, { label: 'Return lines', key: 'return_lines', num: true, format: F.int }, { label: 'Rate', key: 'line_rate', num: true, format: F.pct }, { label: 'Value returned', key: 'returned_value', num: true, format: F.gbpFull }], rows: r.products },
        }, p => hbars(p, { rows: r.products.slice(0, 10).map(x => ({ label: x.description, values: [x.line_rate], extra: [{ name: 'return lines', value: F.int(x.return_lines) }, { name: 'sale lines', value: F.int(x.sale_lines) }] })), series: [{ name: 'return rate', color: C.actual }], format: F.pct })),
        card({
          title: 'Return rate by country', sub: 'Value returned over value sold, for countries with at least 80 orders.',
          table: { columns: [{ label: 'Country', key: 'country' }, { label: 'Orders', key: 'orders', num: true, format: F.int }, { label: 'Sales', key: 'revenue', num: true, format: F.gbpFull }, { label: 'Returned', key: 'returned_value', num: true, format: F.gbpFull }, { label: 'Rate', key: 'value_rate', num: true, format: F.pct }], rows: r.countries },
        }, p => hbars(p, { rows: r.countries.slice(0, 10).map(x => ({ label: x.country, values: [x.value_rate], extra: [{ name: 'orders', value: F.int(x.orders) }] })), series: [{ name: 'return rate', color: C.actual }], format: F.pct })),
        card({
          title: 'How long until a line is fully returned?', sub: 'Days from the sale to the matching return, for the fully reversed lines.', wide: true,
          table: { columns: [{ label: 'Time to return', key: 'bucket' }, { label: 'Lines', key: 'lines', num: true, format: F.int }], rows: r.days_to_return },
        }, p => columnChart(p, { labels: r.days_to_return.map(x => x.bucket), values: r.days_to_return.map(x => x.lines), name: 'lines', label: 'Days to return' }))));
  },

  Forecast(D) {
    const f = D.forecast;
    const row = name => f.accuracy.find(a => a.model === name);
    const gbm = row('Gradient boosting'), base = row(f.best_baseline);
    const weeks = f.total_history.weeks, start = weeks.indexOf(f.test_start);
    const pad = values => Array(start).fill(null).concat(values);
    const all = {
      label: `All ${F.int(f.products)} products`, weeks, units: f.total_history.units,
      forecast: pad(f.weekly_total.map(w => w.gbm)), baseline: pad(f.weekly_total.map(w => w.average4)),
    };
    const options = [all].concat(f.examples.map(e => ({ label: e.description, weeks: e.weeks, units: e.units, forecast: pad(e.forecast), baseline: pad(e.baseline), wape: e.wape, baseWape: e.baseline_wape })));
    let pick = options[0];
    const select = h('select', { id: 'product', onchange: e => { pick = options[e.target.value]; chart.repaint(); } }, options.map((o, i) => h('option', { value: i, text: o.label })));
    const chart = card({
      title: 'Weekly units: actual, forecast and baseline', wide: true,
      sub: 'Each forecast for a week is made from the weeks before it. Use the arrow keys to step through weeks.',
      legend: [{ name: 'Actual', color: C.actual }, { name: 'Gradient boosting forecast', color: C.model }, { name: 'Average of last 4 weeks', color: C.baseline }],
      table: { columns: [{ label: 'Week starting', key: 'week', format: F.week }, { label: 'Actual', key: 'units', num: true, format: F.int }, { label: 'Gradient boosting', key: 'forecast', num: true, format: F.int }, { label: 'Average of last 4 weeks', key: 'baseline', num: true, format: F.int }],
        rows: () => pick.weeks.map((w, i) => ({ week: w, units: pick.units[i], forecast: pick.forecast[i], baseline: pick.baseline[i] })).slice(start) },
    }, p => lineChart(p, {
      labels: pick.weeks, xFormat: F.monthShort, tipTitle: w => 'Week of ' + F.week(w), height: 300, label: 'Weekly demand and forecasts',
      mark: { index: start, label: 'Backtest starts' },
      series: [{ name: 'actual', color: C.actual, values: pick.units }, { name: 'gradient boosting', color: C.model, values: pick.forecast }, { name: 'average of last 4 weeks', color: C.baseline, values: pick.baseline }],
    }));
    return section(
      intro(`One gradient-boosting model forecasts weekly demand for every regularly selling product. It is backtested on the last ${f.test_weeks} weeks, refitted every ${f.refit_every} weeks on the history available at that point, and compared with simple rules any planner could use.`),
      tiles([
        ['Products forecast', F.int(f.products), `${F.pct0(f.revenue_share)} of sales; sold in at least ${f.min_active_weeks} of ${f.weeks} weeks`],
        ['Error, next week', F.pct(gbm.wape), `${F.pct(base.wape)} for the best simple rule`],
        [`Error, next ${f.protection_weeks} weeks`, F.pct(gbm.wape_next), `${F.pct(base.wape_next)} for the best simple rule`],
        ['Bias', (gbm.bias_next >= 0 ? '+' : '') + F.pct(gbm.bias_next), `${F.pct(base.bias_next)} for the best simple rule (${f.protection_weeks} weeks)`],
      ]),
      h('div', { class: 'control' }, h('label', { for: 'product', text: 'Show' }), select),
      grid(chart,
        card({
          title: 'Accuracy by method', wide: true,
          sub: 'Error is the total absolute miss divided by total demand (WAPE), so 60% means the misses add up to 60% of what was sold. Bias is the net over- or under-forecast.',
          table: { columns: [{ label: 'Method', key: 'model' }, { label: 'Error, next week', key: 'wape', num: true, format: F.pct }, { label: 'Bias', key: 'bias', num: true, format: F.pct }, { label: `Error, next ${f.protection_weeks} weeks`, key: 'wape_next', num: true, format: F.pct }, { label: 'Bias', key: 'bias_next', num: true, format: F.pct }], rows: f.accuracy, best: r => r.model === 'Gradient boosting' },
          notes: [
            `Weekly demand per product is lumpy here, because single wholesale orders move a product's week. That is why even the best method misses by ${F.pct0(gbm.wape)} at one week.`,
            `The model's gain over the 4-week average is small at one week (${F.pct(f.wape_reduction_vs_best_baseline)} lower error) and clearer over the ${f.protection_weeks}-week window an order has to cover (${F.pct(1 - gbm.wape_next / base.wape_next)} lower).`,
            'Copying the same week from last year is the worst method: it over-forecasts by a third, because this year sold less per product than last.',
          ],
        })));
  },

  Inventory(D) {
    const v = D.inventory;
    const names = Object.keys(v.frontier);
    const colors = [C.model, C.baseline];
    const at = fill => v.comparison.find(c => c.fill_rate === fill);
    const change = c => c ? (c.stock_reduction >= 0 ? '−' : '+') + F.pct(Math.abs(c.stock_reduction)) : 'n/a';
    return section(
      intro(`Does a better forecast let the business hold less stock for the same service? Each week an order tops stock up to the forecast for the next ${v.protection_weeks} weeks plus a safety margin; it arrives ${v.lead_time_weeks} weeks later. Both policies below use the same safety margin, so only the forecast differs.`),
      tiles([
        ['Products simulated', F.int(v.products), `${v.weeks} weeks, ${F.gbp(v.demand_value)} of demand`],
        ['Stock needed for a 90% fill rate', change(at(0.9)), at(0.9) ? `${F.gbp(at(0.9).model_stock)} against ${F.gbp(at(0.9).baseline_stock)}` : ''],
        ['Stock needed for a 95% fill rate', change(at(0.95)), at(0.95) ? `${F.gbp(at(0.95).model_stock)} against ${F.gbp(at(0.95).baseline_stock)}` : ''],
        ['Stock needed for a 98% fill rate', change(at(0.98)), at(0.98) ? `${F.gbp(at(0.98).model_stock)} against ${F.gbp(at(0.98).baseline_stock)}` : ''],
      ]),
      grid(
        card({
          title: 'Service against stock held', wide: true,
          sub: 'Fill rate (share of demand value served from stock) against average stock value. Each point is one safety margin; up and to the left is better.',
          legend: names.map((n, i) => ({ name: n, color: colors[i] })),
          table: { columns: [{ label: 'Safety factor z', key: 'z', num: true }, { label: 'Fill rate, model forecast', key: 'model_fill', num: true, format: F.pct }, { label: 'Stock, model forecast', key: 'model_stock', num: true, format: F.gbpFull }, { label: 'Fill rate, 4-week average', key: 'baseline_fill', num: true, format: F.pct }, { label: 'Stock, 4-week average', key: 'baseline_stock', num: true, format: F.gbpFull }], rows: v.same_z },
          notes: [
            'The better forecast saves stock at moderate service levels and stops mattering near 98%: up there the safety margin, not the forecast, decides how much is held.',
            'Assumptions: weekly review, a fixed two-week lead time, unmet demand is lost, stock valued at each product\'s median selling price.',
          ],
        }, p => xyChart(p, {
          xFormat: F.gbp, yFormat: F.pct0, xTitle: 'Average stock value', label: 'Fill rate against stock value',
          series: names.map((n, i) => ({ name: n, color: colors[i], points: v.frontier[n].map(q => ({ x: q.average_stock_value, y: q.fill_rate, tip: [{ value: F.pct(q.fill_rate), name: 'fill rate' }, { value: F.gbp(q.average_stock_value), name: 'average stock' }, { value: String(q.z), name: 'safety factor z' }] })) })),
        }))));
  },

  Recommendations(D) {
    const r = D.recommender, [model, base] = r.models;
    let pick = r.examples[0];
    const columns = [
      { label: 'Bought together with', key: 'description' },
      { label: 'Similarity', key: 'similarity', render: v => h('span', {}, h('span', { class: 'meter', style: `width:${Math.round(v * 120)}px` }), ' ' + F.dec(v, 2)) },
      { label: 'Orders containing both', key: 'orders_together', num: true, format: F.int },
    ];
    const box = h('div', { class: 'table-box' });
    const title = h('h3', {});
    const draw = () => { title.textContent = `Frequently bought with ${pick.description}`; box.replaceChildren(dataTable(columns, pick.neighbours)); };
    const select = h('select', { id: 'rec-product', onchange: e => { pick = r.examples[e.target.value]; draw(); } }, r.examples.map((e, i) => h('option', { value: i, text: `${e.description} (${F.int(e.orders)} orders)` })));
    draw();
    return section(
      intro(`Two products are similar when they turn up in the same orders. The test hides one product from each of ${F.int(r.test_baskets)} later orders and asks the recommender to bring it back from the rest of the basket.`),
      tiles([
        ['Hidden product found in the top 10', F.pct(model.hit_rate_at_10), `${F.pct(model.hit_rate_at_5)} in the top 5`],
        ['Same test, recommending best sellers', F.pct(base.hit_rate_at_10), 'the baseline'],
        ['Improvement over the baseline', F.dec(r.lift_at_10, 1) + '×', 'on hit rate in the top 10'],
        ['Products covered', F.int(r.items), `learned from ${F.int(r.train_orders)} orders before ${F.week(r.test_from)}`],
      ]),
      h('div', { class: 'control' }, h('label', { for: 'rec-product', text: 'Product' }), select),
      grid(
        h('figure', { class: 'card wide' }, h('div', { class: 'card-head' }, h('div', {}, title, h('p', { class: 'sub', text: 'Similarity is the number of orders containing both products, divided by the geometric mean of each product\'s order count.' }))), box),
        card({
          title: 'Evaluation on later orders', wide: true,
          table: { columns: [{ label: 'Method', key: 'model' }, { label: 'Hit rate, top 5', key: 'hit_rate_at_5', num: true, format: F.pct }, { label: 'Hit rate, top 10', key: 'hit_rate_at_10', num: true, format: F.pct }, { label: 'Mean reciprocal rank', key: 'mean_reciprocal_rank', num: true, format: v => F.dec(v, 3) }], rows: r.models, best: x => x === model },
          notes: [`Orders with more than ${r.max_basket} different products are left out: they are wholesale restocks that link every product to every other.`, 'The similarity table is built only from orders before the test period, so the test orders are new to it.'],
        })));
  },

  'Data quality'(D) {
    const q = D.quality;
    const kinds = [
      ['Sale', q.line_types.sale, 'A product sold at a positive price'],
      ['Return', q.line_types.return, 'A product line on a cancellation invoice'],
      ['Not a product', q.line_types.non_product, 'Postage, fees, vouchers, manual and test entries'],
      ['Stock adjustment', q.line_types.adjustment, 'Write-offs and zero-price lines'],
      ['Published twice', q.overlap_rows_removed, '1 to 9 December 2010 appears in both sheets of the source'],
    ].map(([kind, lines, meaning]) => ({ kind, lines, meaning, share: lines / q.raw_rows }));
    return section(
      intro('The source is used as published. Every cleaning rule is a line of SQL, and the pipeline stops if any check below fails.'),
      tiles([
        ['Rows in the source', F.int(q.raw_rows), `${q.first_date} to ${q.last_date}`],
        ['Rows published twice and removed', F.int(q.overlap_rows_removed), 'nine days that appear in both sheets'],
        ['Transaction lines kept', F.int(q.fact_rows), 'sales and returns of real products'],
        ['Checks passed', `${q.checks.filter(c => !c.failures).length} of ${q.checks.length}`, 'run on every build'],
      ]),
      grid(
        card({
          title: 'What each source row turned out to be', wide: true,
          table: { columns: [{ label: 'Kind', key: 'kind' }, { label: 'Rows', key: 'lines', num: true, format: F.int }, { label: 'Share', key: 'share', num: true, format: v => F.pct(v, 2) }, { label: 'Meaning', key: 'meaning' }], rows: kinds },
          notes: [
            `${F.int(q.lines_without_customer)} transaction lines have no customer id (${F.pct(q.revenue_share_without_customer)} of sales value). They count in sales and demand, and are left out of customer analysis.`,
            `${F.int(q.reversed_sale_lines)} sale lines (${F.int(q.reversed_units)} units) were later returned in full by the same customer. They stay in gross sales but are not counted as demand.`,
            `${F.int(q.repeated_identical_lines_kept)} lines repeat another line of the same order exactly. They are kept, because a repeated scan cannot be told apart from a duplicate.`,
          ],
        }),
        card({
          title: 'Checks', table: { columns: [{ label: 'Check', key: 'name' }, { label: 'Result', key: 'failures', render: v => v ? `${v} failures` : h('span', { class: 'status', text: 'Pass' }) }], rows: q.checks },
        }),
        card({
          title: 'Warehouse tables', sub: 'Built from scratch by the SQL files on every run.',
          table: { columns: [{ label: 'Table', key: 'table' }, { label: 'Rows', key: 'rows', num: true, format: F.int }], rows: Object.entries(q.tables).map(([table, rows]) => ({ table, rows })) },
        })));
  },
};

// ---------- page ----------
(async function main() {
  const D = await (await fetch('data/data.json')).json();
  document.getElementById('lede').textContent =
    `${F.int(D.quality.raw_rows)} real transactions from a UK online retailer, turned into a SQL warehouse, customer and returns analysis, demand forecasts, a stock simulation and product recommendations.`;
  document.getElementById('foot').textContent =
    `Data: ${D.source.name}, ${D.source.publisher}, ${D.source.licence} (doi:${D.source.doi}). Amounts are in pounds sterling. Built by S Harshni.`;

  const main = document.getElementById('main'), nav = document.getElementById('tabs');
  const names = Object.keys(TABS), built = {};
  const slug = n => n.toLowerCase().replace(/\s+/g, '-');
  function open(name) {
    hideTip();
    for (const b of nav.children) b.setAttribute('aria-selected', String(b.textContent === name));
    for (const el of main.children) el.hidden = true;
    built[name] ??= main.appendChild(TABS[name](D));
    built[name].hidden = false;
    history.replaceState(null, '', '#' + slug(name));
    renderAll();
  }
  nav.append(...names.map(n => h('button', { type: 'button', role: 'tab', text: n, onclick: () => open(n) })));
  open(names.find(n => '#' + slug(n) === location.hash) || names[0]);

  let timer;
  addEventListener('resize', () => { clearTimeout(timer); timer = setTimeout(renderAll, 150); });
  const themeBtn = document.getElementById('theme');
  const isDark = () => (document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
  themeBtn.textContent = isDark() ? 'Light mode' : 'Dark mode';
  themeBtn.addEventListener('click', () => {
    document.documentElement.dataset.theme = isDark() ? 'light' : 'dark';
    themeBtn.textContent = isDark() ? 'Light mode' : 'Dark mode';
  });
})();
