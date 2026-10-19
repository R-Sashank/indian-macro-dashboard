/* Indian Macro Dashboard - front end. Static; reads site/data/*.json. */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const FREQ = { D: 'Daily', W: 'Weekly', M: 'Monthly', Q: 'Quarterly', A: 'Annual' };
const RANGES = [['1', '1Y'], ['5', '5Y'], ['10', '10Y'], ['max', 'MAX']];
const TABLE_ROWS = 1000;

let IDX = null;
let DATA_SOURCE = 'local'; // 'live' (GitHub), 'bundled' (fallback copy shipped with the deploy) or 'local'
const CACHE = new Map();
const S = { rec: null, view: 0, range: 'max', table: false, chart: null, hidden: new Set() };

/* ------------------------------------------------------------ formatting */

const nf = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });
const unitsDisp = (u) => (u == null ? '' : String(u).replace(/\$\$/g, '$').trim());
const fmtNum = (v) => (v == null ? '' : nf.format(v));
function fmtVal(v, u) {
  if (v == null) return '–';
  return u === 'Per cent' ? `${nf.format(v)}%` : nf.format(v) + (u ? ' ' + unitsDisp(u) : '');
}
function fmtDate(ts, freq) {
  const d = new Date(ts * 1000);
  const m = MONTHS[d.getUTCMonth()], y = d.getUTCFullYear();
  if (freq === 'M') return `${m} ${y}`;
  if (freq === 'A') return String(y);
  if (freq === 'Q') return `Quarter ended ${m} ${y}`;
  return `${d.getUTCDate()} ${m} ${y}`;
}
/** On-page credit names CMIE only; the full credit stays in the footer and in downloaded files. */
const srcShort = (s) => String(s || IDX.source).replace(/,\s*1 Finance Research/i, '');
const isoDay = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);
const freqLabel = (f) => FREQ[f] || '';

/* ---------------------------------------------------------------- data */

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
/** Load a data file from the configured remote base, falling back to the bundled copy. */
async function getData(path) {
  const bases = window.DATA_BASE ? [window.DATA_BASE, 'data/'] : ['data/'];
  let last;
  for (const b of bases) {
    try {
      const data = await getJSON(b + path);
      if (path === 'index.json') DATA_SOURCE = b === 'data/' ? (window.DATA_BASE ? 'bundled' : 'local') : 'live';
      return data;
    } catch (e) { last = e; }
  }
  throw last;
}
async function getRec(slug) {
  if (!CACHE.has(slug)) CACHE.set(slug, getData(`i/${slug}.json`).catch((e) => { CACHE.delete(slug); throw e; }));
  return CACHE.get(slug);
}
const catName = (slug) => IDX.categories.find((c) => c.slug === slug)?.name || slug;
const titleOf = (slug) => IDX.indicators[slug]?.name || IDX.indicators[slug]?.title || slug;

/* --------------------------------------------------------------- router */

function route() {
  const h = location.hash.replace(/^#\/?/, '');
  const [kind, arg] = h.split('/');
  if (kind === 'i' && arg) return openIndicator(arg);
  showHome(kind === 'c' ? arg : null);
}
window.addEventListener('hashchange', route);

/* ----------------------------------------------------------------- home */

function sparkSvg(vals) {
  if (!vals || vals.length < 2) return '';
  const w = 84, h = 30, p = 2;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const pts = vals.map((v, i) => `${(p + (i * (w - 2 * p)) / (vals.length - 1)).toFixed(1)},${(h - p - ((v - lo) / span) * (h - 2 * p)).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}"/></svg>`;
}

function cardHtml(slug) {
  const it = IDX.indicators[slug];
  if (!it) return '';
  const chg = it.latest_yoy == null ? '' : `<span class="chg">${it.latest_yoy >= 0 ? '▲' : '▼'} ${fmtNum(Math.abs(it.latest_yoy))}% YoY</span>`;
  let val;
  if (it.kind === 'curve') val = '<span class="val" style="font-size:15px;font-weight:600">Yield curve by maturity</span>';
  else if (it.kind === 'table') val = `<span class="val">${fmtNum(it.n_points)}</span><span class="u">${slug.includes('calendar') ? 'scheduled releases' : 'markets'}</span>`;
  else val = `<span class="val">${fmtNum(it.latest_value)}</span><span class="u">${esc(it.units === 'Per cent' ? '%' : unitsDisp(it.units))}</span>`;
  const when = it.latest_date ? fmtDate(Date.parse(it.latest_date) / 1000, it.frequency) : (it.as_of ? `As of ${it.as_of}` : '');
  return `<a class="card" href="#/i/${slug}">
    <h3>${esc(it.name)}</h3>
    <div class="row"><div>${val}</div>${sparkSvg(it.spark)}</div>
    <div class="meta"><span>${esc(when)}</span>${chg}${it.phase ? `<span>${esc(it.phase)}</span>` : ''}${it.kind !== 'table' && freqLabel(it.frequency) ? `<span>${freqLabel(it.frequency)}</span>` : ''}${it.quality_note ? '<span class="tag-warn">check data</span>' : ''}</div>
  </a>`;
}

function showHome(scrollTo) {
  destroyChart();
  document.title = 'Indian Macro Dashboard';
  const total = Object.keys(IDX.indicators).length;
  const pills = IDX.categories.map((c) => `<a class="pill" href="#/c/${c.slug}" data-c="${c.slug}">${esc(c.name)}</a>`).join('');
  const sections = IDX.categories.map((c) => `
    <section class="cat" id="cat-${c.slug}">
      <div class="cat-head">
        <h2>${esc(c.name)}</h2><span class="count">${c.indicators.length} indicators</span>
        <button class="btn" data-dl-cat="${c.slug}">Download category (.xlsx)</button>
        ${c.description ? `<p>${esc(c.description)}</p>` : ''}
      </div>
      <div class="grid">${c.indicators.map(cardHtml).join('')}</div>
    </section>`).join('');
  $('#app').innerHTML = `
    <section class="hero">
      <h1>India's high-frequency economic indicators</h1>
      <p>Every indicator with its full history, charted and explained, and available as Excel or CSV.</p>
      <div class="facts"><span><b>${total}</b>datasets</span><span><b>${IDX.categories.length}</b>sections</span><span><b>Excel · CSV</b>for every series</span></div>
    </section>
    <nav class="pills" aria-label="Categories">${pills}</nav>
    ${sections}`;
  $('#app').querySelectorAll('[data-dl-cat]').forEach((b) => b.addEventListener('click', () => downloadCategory(b)));
  if (scrollTo) {
    const t = $(`#cat-${scrollTo}`);
    if (t) t.scrollIntoView();
    $('.pill[data-c="' + scrollTo + '"]')?.classList.add('on');
  } else window.scrollTo(0, 0);
}

/* --------------------------------------------------------------- search */

function setupSearch() {
  const q = $('#q'), box = $('#qres');
  let sel = -1, hits = [];
  const hide = () => { box.hidden = true; sel = -1; };
  const draw = () => {
    const t = q.value.trim().toLowerCase();
    if (!t) return hide();
    const words = t.split(/\s+/);
    hits = Object.values(IDX.indicators).filter((it) => {
      const hay = (it.name + ' ' + it.title + ' ' + it.slug).toLowerCase();
      return words.every((w) => hay.includes(w));
    }).slice(0, 12);
    box.innerHTML = hits.length
      ? hits.map((h, i) => `<a href="#/i/${h.slug}" data-i="${i}">${esc(h.name)}<small>${esc(h.categories.map(catName).join(' · '))}</small></a>`).join('')
      : '<div class="none">No indicators match.</div>';
    box.hidden = false; sel = -1;
  };
  q.addEventListener('input', draw);
  q.addEventListener('focus', draw);
  q.addEventListener('keydown', (e) => {
    const links = [...box.querySelectorAll('a')];
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % (links.length || 1);
      links.forEach((l, i) => l.classList.toggle('on', i === sel));
    } else if (e.key === 'Enter' && hits.length) {
      location.hash = `#/i/${hits[Math.max(sel, 0)].slug}`; q.blur(); hide();
    } else if (e.key === 'Escape') { hide(); q.blur(); }
  });
  box.addEventListener('click', () => { hide(); q.value = ''; });
  document.addEventListener('click', (e) => { if (!e.target.closest('.searchbox')) hide(); });
}

/* --------------------------------------------------------------- detail */

async function openIndicator(slug) {
  let rec;
  try { rec = await getRec(slug); } catch (e) {
    $('#app').innerHTML = `<p class="empty">Could not load “${esc(slug)}”. <a href="#/">Back to all indicators</a></p>`;
    return;
  }
  destroyChart();
  if (rec.kind === 'table') return openTablePage(rec);
  Object.assign(S, { rec, view: 0, range: 'max', table: false, hidden: new Set() });
  const idx = IDX.indicators[slug] || {};
  document.title = `${rec.title || idx.name} · Indian Macro Dashboard`;
  const ix = indexExtras(rec);

  const cats = (idx.categories || []).map((c) => `<a class="chip" href="#/c/${c}">${esc(catName(c))}</a>`).join('');
  const head = rec.headline && (rec.headline.Absolute || Object.values(rec.headline)[0]);
  const related = (rec.related || []).filter((r) => r.slug !== slug).map((r) =>
    IDX.indicators[r.slug] ? `<a class="chip" href="#/i/${r.slug}">${esc(titleOf(r.slug))}</a>` : `<span class="chip plain">${esc(r.title || r.slug)}</span>`).join('');
  const qna = (rec.qna || []).map((q) => `<div><h2>${esc(q.question)}</h2>${String(q.answer || '').split('^').map((p) => `<p>${esc(p)}</p>`).join('')}</div>`).join('');

  $('#app').innerHTML = `
    <div class="detail">
      <nav class="crumbs"><a href="#/">All indicators</a>${idx.categories?.[0] ? ` &nbsp;/&nbsp; <a href="#/c/${idx.categories[0]}">${esc(catName(idx.categories[0]))}</a>` : ''}</nav>
      <h1>${esc(rec.title || idx.name)}</h1>
      ${head ? `<p class="headline">${esc(head)}</p>` : rec.description ? `<p class="headline">${esc(rec.description)}</p>` : ''}
      <div class="chips">${cats}${freqLabel(rec.frequency) ? `<span class="chip plain">${freqLabel(rec.frequency)}</span>` : ''}</div>
      ${rec.quality_note ? `<div class="note"><strong>Data check:</strong> ${esc(rec.quality_note)}</div>` : ''}
      ${ix.top}
      <section class="panel">
        <div class="controls" id="controls"></div>
        <div class="stats" id="stats"></div>
        <div class="legend" id="legend"></div>
        <div class="chartwrap${rec.kind === 'curve' ? ' curve' : ''}"><canvas id="chart" role="img" aria-label="Chart of ${esc(rec.title || '')}"></canvas></div>
        <div class="foot-row"><span id="asof"></span><span>Source: ${esc(srcShort(rec.source))}</span></div>
        <div id="tablebox"></div>
      </section>
      ${ix.bottom}
      ${qna ? `<section class="about">${qna}</section>` : ''}
      ${ix.events}
      ${related ? `<section class="related"><h2>Related indicators</h2><div class="chips">${related}</div></section>` : ''}
    </div>`;
  drawControls();
  refresh();
  window.scrollTo(0, 0);
}

function drawControls() {
  const rec = S.rec, c = $('#controls');
  let html = '';
  if (rec.kind === 'timeseries') {
    if (rec.views.length > 1) {
      html += `<div class="seg" id="viewseg">${rec.views.map((v, i) => `<button data-v="${i}" class="${i === S.view ? 'on' : ''}">${esc(v.label || 'View')}</button>`).join('')}</div>`;
    }
    html += `<div class="seg" id="rangeseg">${RANGES.map(([k, l]) => `<button data-r="${k}" class="${k === S.range ? 'on' : ''}">${l}</button>`).join('')}</div>`;
  }
  html += `<span class="sp"></span><button class="btn" id="tblbtn">Table</button><button class="btn" id="csvbtn">CSV</button><button class="btn primary" id="xlsxbtn">Download Excel</button>`;
  c.innerHTML = html;
  $('#viewseg')?.addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) { S.view = +b.dataset.v; S.hidden.clear(); refresh(); } });
  $('#rangeseg')?.addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) { S.range = b.dataset.r; refresh(); } });
  $('#tblbtn').addEventListener('click', () => { S.table = !S.table; refresh(); });
  $('#csvbtn').addEventListener('click', downloadCsv);
  $('#xlsxbtn').addEventListener('click', downloadXlsx);
}

const rangeLabel = () => (S.rec.kind === 'curve' ? 'snapshot' : S.rec.kind === 'table' ? 'all' : RANGES.find((r) => r[0] === S.range)[1]);
const noCut = (rec) => rec.kind === 'curve' || rec.kind === 'table';

function cutoffMs(rec) {
  if (S.range === 'max') return -Infinity;
  let end = -Infinity;
  for (const v of rec.views) for (const s of v.series) if (s.pts.length) end = Math.max(end, s.pts[s.pts.length - 1][0] * 1000);
  const d = new Date(end);
  d.setUTCFullYear(d.getUTCFullYear() - Number(S.range));
  return d.getTime();
}

function refresh() {
  const rec = S.rec;
  $('#rangeseg')?.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.r === S.range));
  $('#viewseg')?.querySelectorAll('button').forEach((b) => b.classList.toggle('on', +b.dataset.v === S.view));
  $('#xlsxbtn').textContent = `Download Excel (${rec.kind === 'curve' ? 'snapshot' : rangeLabel()})`;
  $('#tblbtn').textContent = S.table ? 'Hide table' : 'Table';
  if (rec.kind === 'curve') drawCurve(); else drawTimeseries();
  drawTable();
}

/* ---- chart plumbing ---- */

function destroyChart() { if (S.chart) { S.chart.destroy(); S.chart = null; } }

if (window.Chart) Chart.defaults.font.family = "'IBM Plex Sans', system-ui, sans-serif";

const crosshair = {
  id: 'crosshair',
  afterDatasetsDraw(chart) {
    const a = chart.tooltip?.getActiveElements?.();
    if (!a || !a.length) return;
    const x = a[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = cssv('--axis'); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  },
};

function baseOptions(unitsText) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: cssv('--raise'), titleColor: cssv('--ink'), bodyColor: cssv('--ink-2'),
        borderColor: cssv('--border'), borderWidth: 1, padding: 10, boxPadding: 4,
      },
    },
    scales: {
      y: {
        grid: { color: cssv('--grid') }, border: { display: false },
        ticks: { color: cssv('--muted'), callback: (v) => nf.format(v) },
        title: unitsText ? { display: true, text: unitsText, color: cssv('--muted'), font: { size: 12 } } : { display: false },
      },
    },
  };
}

function drawTimeseries() {
  const rec = S.rec, view = rec.views[S.view], cut = cutoffMs(rec);
  const idx = IDX.indicators[rec.slug] || {};
  const colors = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6'].map(cssv);
  const series = view.series.map((s) => ({ s, pts: s.pts.filter((p) => p[0] * 1000 >= cut) }));
  const unitSet = [...new Set(view.series.map((s) => s.units))];
  const unitsText = unitSet.length === 1 ? (unitSet[0] === 'Per cent' ? '%' : unitsDisp(unitSet[0])) : '';
  const totalPts = Math.max(0, ...series.map((x) => x.pts.length));
  const asBar = idx.graph === 'bar' && series.length === 1 && totalPts <= 400;

  // stats row + legend
  $('#stats').innerHTML = series.map((x, i) => {
    const last = x.pts[x.pts.length - 1];
    return `<div class="stat"><div class="big">${last ? fmtNum(last[1]) : '–'}<span class="u">${esc(x.s.units === 'Per cent' ? '%' : unitsDisp(x.s.units))}</span></div>
      <div class="lab">${series.length > 1 ? `<span class="sw" style="background:${colors[i % 6]}"></span>` : ''}${esc(x.s.name === 'Value' || x.s.name === 'YoY change' ? (view.label || x.s.name) : x.s.name)}${last ? ' · ' + fmtDate(last[0], rec.frequency) : ''}</div></div>`;
  }).join('');
  const lg = $('#legend');
  lg.innerHTML = series.length > 1
    ? series.map((x, i) => `<button data-s="${i}" class="${S.hidden.has(i) ? 'off' : ''}"><span class="sw" style="background:${colors[i % 6]}"></span>${esc(x.s.name)}</button>`).join('') : '';
  lg.onclick = (e) => { const b = e.target.closest('button'); if (!b) return; const i = +b.dataset.s; S.hidden.has(i) ? S.hidden.delete(i) : S.hidden.add(i); drawTimeseries(); };

  const all = series.flatMap((x) => x.pts.map((p) => p[1]));
  const nonNeg = all.length && Math.min(...all) >= 0;
  const datasets = series.map((x, i) => ({
    label: x.s.name, data: x.pts.map((p) => ({ x: p[0] * 1000, y: p[1] })),
    borderColor: colors[i % 6], backgroundColor: asBar ? colors[i % 6] : colors[i % 6] + '1c',
    borderWidth: asBar ? 0 : 2, borderRadius: asBar ? 2 : 0, pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: colors[i % 6],
    tension: 0, fill: !asBar && series.length === 1 && nonNeg ? 'origin' : false, hidden: S.hidden.has(i),
    spanGaps: true, parsing: false, normalized: true,
  }));
  const opts = baseOptions(unitsText);
  opts.scales.x = {
    type: 'time', grid: { display: false }, border: { color: cssv('--axis') }, offset: asBar,
    ticks: { color: cssv('--muted'), maxTicksLimit: 8, maxRotation: 0, autoSkipPadding: 18 },
  };
  opts.plugins.decimation = { enabled: !asBar, algorithm: 'lttb', samples: 1200 };
  opts.plugins.tooltip.callbacks = {
    title: (items) => (items[0] ? fmtDate(items[0].parsed.x / 1000, rec.frequency) : ''),
    label: (c) => ` ${c.dataset.label}: ${fmtVal(c.parsed.y, series[c.datasetIndex].s.units)}`,
  };
  destroyChart();
  if (!totalPts) { $('#chart').parentElement.insertAdjacentHTML('afterbegin', ''); }
  S.chart = new Chart($('#chart'), { type: asBar ? 'bar' : 'line', data: { datasets }, options: opts, plugins: [crosshair] });

  const lastTs = Math.max(...view.series.map((s) => (s.pts.length ? s.pts[s.pts.length - 1][0] : -Infinity)));
  $('#asof').textContent = `Data through ${fmtDate(lastTs, rec.frequency)} · refreshed ${new Date(rec.scraped_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}`;
}

function drawCurve() {
  const c = S.rec.curve, colors = ['--s1', '--s2', '--s3'].map(cssv);
  $('#stats').innerHTML = c.snapshots.map((sn, i) => {
    const spread = sn.values[c.labels.indexOf('10Y')] - sn.values[c.labels.indexOf('1Y')];
    return `<div class="stat"><div class="big">${isFinite(spread) ? fmtNum(spread) : '–'}<span class="u">pp</span></div><div class="lab"><span class="sw" style="background:${colors[i]}"></span>10Y minus 1Y · ${esc(sn.name)} (${esc(sn.date)})</div></div>`;
  }).join('');
  $('#legend').innerHTML = '';
  const opts = baseOptions('% yield');
  opts.scales.x = { grid: { display: false }, border: { color: cssv('--axis') }, ticks: { color: cssv('--muted') }, title: { display: true, text: 'Maturity', color: cssv('--muted'), font: { size: 12 } } };
  opts.plugins.legend = { display: true, labels: { color: cssv('--ink-2'), usePointStyle: true, boxWidth: 8 } };
  opts.plugins.tooltip.callbacks = { label: (x) => ` ${x.dataset.label}: ${nf.format(x.parsed.y)}%` };
  destroyChart();
  S.chart = new Chart($('#chart'), {
    type: 'line',
    data: { labels: c.labels, datasets: c.snapshots.map((sn, i) => ({ label: `${sn.name} (${sn.date})`, data: sn.values, borderColor: colors[i], backgroundColor: colors[i], borderWidth: 2, pointRadius: 4, pointHoverRadius: 6, tension: 0.25 })) },
    options: opts, plugins: [crosshair],
  });
  $('#asof').textContent = `Snapshot of ${c.snapshots[0].date} · refreshed ${new Date(S.rec.scraped_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}`;
}

/* ---- table view ---- */

function drawTable() {
  const box = $('#tablebox');
  if (!S.table) { box.innerHTML = ''; return; }
  const t = buildTable(S.rec, S.rec.kind === 'curve' ? -Infinity : cutoffMs(S.rec), 'display');
  const rows = t.rows.slice().reverse();
  const shown = rows.slice(0, TABLE_ROWS);
  const body = shown.map((r) => `<tr>${r.map((c, i) => `<td>${i === 0 || typeof c === 'string' ? esc(c) : fmtNum(c)}</td>`).join('')}</tr>`).join('');
  box.innerHTML = `<div class="tablewrap"><table><thead><tr>${t.headers.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table>
    ${rows.length > shown.length ? `<div class="tablenote">Showing the latest ${shown.length.toLocaleString()} of ${rows.length.toLocaleString()} rows. Download Excel or CSV for everything in this range.</div>` : ''}</div>`;
}

/* ---- index pages: phase, interpretation, outlook, events ---- */

const humanize = (k) => k.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/^./, (c) => c.toUpperCase());
const bullets = (a) => (a && a.length ? `<ul>${a.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '');

function indexExtras(rec) {
  const m = rec.index_meta;
  if (!m) return { top: '', bottom: '', events: '' };
  const top = `<section class="phase">
    <div><div class="lab">Current phase${m.last_updated ? ` · updated ${esc(m.last_updated)}` : ''}</div><div class="big">${esc(m.phase)}</div><p>${esc(m.phase_summary)}</p></div>
    <div><div class="lab">Success rate</div><div class="big">${esc(m.success_rate)}</div><p>${esc(m.success_rate_description)}</p></div>
  </section>`;
  const bottom = `<section class="about">
    ${m.interpretation?.length ? `<div><h2>How to read the index</h2>${m.interpretation.map((b) => `<p><strong>${esc(b.title)}.</strong> ${esc(b.description)}</p>`).join('')}</div>` : ''}
    ${m.recent_updates?.length ? `<div><h2>Recent updates</h2>${bullets(m.recent_updates)}</div>` : ''}
    ${m.near_term_outlook?.length ? `<div><h2>Near-term outlook</h2>${bullets(m.near_term_outlook)}</div>` : ''}
    ${m.insights?.length ? `<div><h2>How to use it</h2>${m.insights.map((i) => `<p><strong>${esc(i.label)}</strong> ${esc(i.definition)}</p>`).join('')}</div>` : ''}
  </section>`;
  const events = m.historical_events?.length ? `<section class="about"><div><h2>Historical events</h2>
    <div class="events">${m.historical_events.map((e) => `<article class="event"><div class="when">${esc([e.month, e.year].filter(Boolean).join(' '))}</div><h3>${esc(e.title)}</h3>
      ${Object.entries(e).filter(([k, v]) => !['month', 'year', 'title'].includes(k) && typeof v === 'string' && v.trim()).map(([k, v]) => `<p>${k === 'description' ? '' : `<strong>${esc(humanize(k))}:</strong> `}${esc(v)}</p>`).join('')}</article>`).join('')}</div></div></section>` : '';
  return { top, bottom, events };
}

/* ---- table pages: global P/E ratios, economic calendar ---- */

function tableCols(rec) { return rec.columns.map((c, i) => ({ ...c, i })).filter((c) => !c.hidden); }

function openTablePage(rec) {
  Object.assign(S, { rec, view: 0, range: 'max', table: false, hidden: new Set(), sort: { col: -1, dir: 1 }, filter: '' });
  const idx = IDX.indicators[rec.slug] || {};
  document.title = `${rec.title} · Indian Macro Dashboard`;
  const notes = (rec.notes || []).filter((n) => n !== rec.description).map((p) => `<p>${esc(p)}</p>`).join('');
  const cats = (idx.categories || []).map((c) => `<a class="chip" href="#/c/${c}">${esc(catName(c))}</a>`).join('');
  $('#app').innerHTML = `<div class="detail">
    <nav class="crumbs"><a href="#/">All indicators</a>${idx.categories?.[0] ? ` &nbsp;/&nbsp; <a href="#/c/${idx.categories[0]}">${esc(catName(idx.categories[0]))}</a>` : ''}</nav>
    <h1>${esc(rec.title)}</h1>
    ${rec.description ? `<p class="headline">${esc(rec.description)}</p>` : ''}
    <div class="chips">${cats}</div>
    <section class="panel">
      <div class="controls">
        <input id="tfilter" class="filter" type="search" placeholder="Filter rows…" aria-label="Filter rows">
        <span class="sp"></span><button class="btn" id="csvbtn">CSV</button><button class="btn primary" id="xlsxbtn">Download Excel</button>
      </div>
      <div class="tablewrap big tbl2" id="tbl"></div>
      <div class="foot-row"><span id="asof">${rec.as_of ? `As of ${esc(rec.as_of)} · ` : ''}refreshed ${new Date(rec.scraped_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}</span><span>Source: ${esc(srcShort(rec.source))}</span></div>
    </section>
    ${notes ? `<section class="about"><div>${notes}</div></section>` : ''}
  </div>`;
  $('#tfilter').addEventListener('input', (e) => { S.filter = e.target.value.toLowerCase(); drawTablePage(); });
  $('#csvbtn').addEventListener('click', downloadCsv);
  $('#xlsxbtn').addEventListener('click', downloadXlsx);
  $('#tbl').addEventListener('click', (e) => {
    const clamp = e.target.closest('.clamp');
    if (clamp) { clamp.classList.toggle('open'); return; }
    const th = e.target.closest('th[data-c]');
    if (!th) return;
    const c = +th.dataset.c;
    S.sort = S.sort.col === c ? { col: c, dir: -S.sort.dir } : { col: c, dir: 1 };
    drawTablePage();
  });
  drawTablePage();
  window.scrollTo(0, 0);
}

function drawTablePage() {
  const rec = S.rec, cols = tableCols(rec);
  const slugCol = rec.columns.findIndex((c) => c.hidden && /slug/i.test(c.label));
  let rows = rec.rows.filter((r) => !S.filter || r.some((v) => String(v ?? '').toLowerCase().includes(S.filter)));
  if (S.sort.col >= 0) {
    const c = S.sort.col, num = rec.columns[c].num;
    rows = rows.slice().sort((a, b) => {
      const x = a[c], y = b[c];
      if (x == null || x === '') return 1;
      if (y == null || y === '') return -1;
      return (num ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })) * S.sort.dir;
    });
  }
  const head = cols.map((c) => `<th data-c="${c.i}" class="${c.num ? 'num' : ''}" style="cursor:pointer" title="Sort">${esc(c.label)}${S.sort.col === c.i ? (S.sort.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('');
  const body = rows.map((r) => `<tr>${cols.map((c) => {
    const v = r[c.i];
    let cell = v == null || v === '' ? '–' : c.num ? fmtNum(v) : esc(v);
    if (slugCol >= 0 && /^indicator$/i.test(c.label) && IDX.indicators[r[slugCol]]) cell = `<a href="#/i/${esc(r[slugCol])}">${esc(v)}</a>`;
    const long = typeof v === 'string' && v.length > 60;
    return `<td class="${c.num ? 'num' : ''}${long ? ' wrap' : ''}">${long ? `<div class="clamp" title="Click to expand">${cell}</div>` : cell}</td>`;
  }).join('')}</tr>`).join('');
  $('#tbl').innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${cols.length}" class="muted">No rows match.</td></tr>`}</tbody></table>
    <div class="tablenote">${rows.length} of ${rec.rows.length} rows${S.filter ? ' (filtered)' : ''}. Downloads always include all rows.</div>`;
}

/* ------------------------------------------------------------- exports */

/** Wide table: date + one column per series across all views. mode: 'display' | 'iso' | 'date' */
function buildTable(rec, cutMs, mode) {
  if (rec.kind === 'table') {
    const cols = tableCols(rec);
    const conv = (v) => (mode === 'date' && typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v)
      ? new Date(+v.slice(0, 4), +v.slice(5, 7) - 1, +v.slice(8, 10), 12) : v);
    return { headers: cols.map((c) => c.label), rows: rec.rows.map((r) => cols.map((c) => conv(r[c.i]))) };
  }
  if (rec.kind === 'curve') {
    const c = rec.curve;
    return {
      headers: ['Maturity', ...c.snapshots.map((s) => `${s.name} (${s.date}) – % yield`)],
      rows: c.labels.map((l, i) => [l, ...c.snapshots.map((s) => s.values[i])]),
    };
  }
  const cols = [];
  const seen = new Map();
  for (const v of rec.views) for (const s of v.series) {
    let h = s.units ? `${s.name} (${unitsDisp(s.units)})` : s.name;
    if (seen.has(h)) h = `${v.label || s.name} – ${h}`;
    seen.set(h, 1);
    cols.push({ h, map: new Map(s.pts.map((p) => [p[0], p[1]])), ref: new Map(s.pts.filter((p) => p[2]).map((p) => [p[0], p[2]])) });
  }
  const hasRef = cols.some((c) => c.ref.size);
  const ts = [...new Set(cols.flatMap((c) => [...c.map.keys()]))].filter((t) => t * 1000 >= cutMs).sort((a, b) => a - b);
  const dateCell = (t) => {
    if (mode === 'display') return fmtDate(t, rec.frequency);
    if (mode === 'iso') return isoDay(t);
    const d = new Date(t * 1000);
    return new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 12); // local noon: no timezone day-shift in Excel
  };
  return {
    headers: ['Date', ...(hasRef ? ['Reference date'] : []), ...cols.map((c) => c.h)],
    rows: ts.map((t) => [dateCell(t), ...(hasRef ? [cols.map((c) => c.ref.get(t)).find(Boolean) || ''] : []), ...cols.map((c) => (c.map.has(t) ? c.map.get(t) : null))]),
  };
}

function addDataSheet(wb, name, rec, cutMs) {
  const t = buildTable(rec, cutMs, 'date');
  const ws = XLSX.utils.aoa_to_sheet([t.headers, ...t.rows], { cellDates: true });
  for (const k of Object.keys(ws)) if (k[0] !== '!' && ws[k].t === 'd') ws[k].z = 'yyyy-mm-dd';
  ws['!cols'] = t.headers.map((h, i) => ({ wch: i === 0 ? 13 : Math.max(14, Math.min(34, h.length + 2)) }));
  ws['!freeze'] = { xSplit: 0, ySplit: 1 };
  XLSX.utils.book_append_sheet(wb, ws, name);
  return t.rows.length;
}

function infoSheet(rec, rangeText, rows) {
  const idx = IDX.indicators[rec.slug] || {};
  const info = [
    ['Indicator', rec.title || idx.name],
    ['Category', (idx.categories || []).map(catName).join('; ')],
    ['Frequency', freqLabel(rec.frequency) || ''],
    ['Range exported', rangeText],
    ['Rows', rows],
    ['Source', rec.source || IDX.source],
    ['Original page', rec.page_url],
    ['Mirrored at (UTC)', rec.scraped_at],
    ['Data check', rec.quality_note || ''],
    ['Description', rec.description || ''],
  ];
  const ws = XLSX.utils.aoa_to_sheet(info);
  ws['!cols'] = [{ wch: 18 }, { wch: 100 }];
  return ws;
}

function aboutSheet(rec) {
  if (!rec.qna?.length) return null;
  const rows = [['Topic', 'Explanation']];
  for (const q of rec.qna) for (const p of String(q.answer || '').split('^')) rows.push([q.question, p]);
  const ws = XLSX.utils.aoa_to_sheet(rows);
  ws['!cols'] = [{ wch: 50 }, { wch: 120 }];
  return ws;
}

const stamp = () => new Date().toISOString().slice(0, 10);

function downloadXlsx() {
  const rec = S.rec, wb = XLSX.utils.book_new();
  const rows = addDataSheet(wb, 'Data', rec, rec.kind === 'curve' ? -Infinity : cutoffMs(rec));
  XLSX.utils.book_append_sheet(wb, infoSheet(rec, rangeLabel() === 'MAX' ? 'Full history' : rangeLabel(), rows), 'Info');
  const about = aboutSheet(rec);
  if (about) XLSX.utils.book_append_sheet(wb, about, 'About');
  XLSX.writeFile(wb, `${rec.slug}_${rangeLabel()}_${stamp()}.xlsx`, { cellDates: true });
}

function downloadCsv() {
  const rec = S.rec;
  const t = buildTable(rec, rec.kind === 'curve' ? -Infinity : cutoffMs(rec), 'iso');
  const q = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v);
  const csv = [t.headers, ...t.rows].map((r) => r.map(q).join(',')).join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }));
  a.download = `${rec.slug}_${rangeLabel()}_${stamp()}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function downloadCategory(btn) {
  const cat = IDX.categories.find((c) => c.slug === btn.dataset.dlCat);
  const label = btn.textContent;
  btn.disabled = true;
  try {
    const wb = XLSX.utils.book_new();
    const contents = [['Indicator', 'Sheet', 'Frequency', 'Rows', 'Latest date', 'Source']];
    const used = new Set();
    let i = 0;
    for (const slug of cat.indicators) {
      btn.textContent = `Preparing ${++i}/${cat.indicators.length}…`;
      const rec = await getRec(slug);
      let name = (IDX.indicators[slug]?.name || slug).replace(/[\[\]:*?/\\]/g, ' ').slice(0, 31).trim();
      let n = name, k = 2;
      while (used.has(n.toLowerCase())) n = `${name.slice(0, 28)} ${k++}`;
      used.add(n.toLowerCase());
      const rows = addDataSheet(wb, n, rec, -Infinity);
      contents.push([IDX.indicators[slug]?.name || slug, n, freqLabel(rec.frequency), rows, IDX.indicators[slug]?.latest_date || '', rec.source || IDX.source]);
      await new Promise((r) => setTimeout(r, 0));
    }
    const cw = XLSX.utils.aoa_to_sheet([['Category', cat.name], ['Source', IDX.source], ['Exported', stamp()], [], ...contents]);
    cw['!cols'] = [{ wch: 52 }, { wch: 30 }, { wch: 12 }, { wch: 9 }, { wch: 12 }, { wch: 44 }];
    XLSX.utils.book_append_sheet(wb, cw, 'Contents');
    wb.SheetNames.unshift(wb.SheetNames.pop());
    XLSX.writeFile(wb, `${cat.slug}_${stamp()}.xlsx`, { cellDates: true });
  } finally { btn.textContent = label; btn.disabled = false; }
}

/* ------------------------------------------------------------- theme + boot */

function applyTheme(t) {
  if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme');
}
function setupTheme() {
  try { applyTheme(localStorage.getItem('theme')); } catch (e) { /* storage unavailable */ }
  $('#theme').addEventListener('click', () => {
    const dark = (document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
    const next = dark ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('theme', next); } catch (e) { /* ignore */ }
    if (S.chart && S.rec && $('#chart')) refresh();
  });
}

async function boot() {
  setupTheme();
  try {
    IDX = await getData('index.json');
  } catch (e) {
    $('#app').innerHTML = '<p class="empty">Data not found. Run <code>python scraper/scrape.py</code> first.</p>';
    return;
  }
  const when = new Date(IDX.generated_at).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
  $('#foot-meta').textContent = DATA_SOURCE === 'bundled'
    ? `Warning: live data could not be reached, so this page is showing a saved copy from ${when}. Reload later for the latest.`
    : `Latest data update: ${when}${DATA_SOURCE === 'live' ? ' (live, checked every 2 hours).' : '.'}`;
  setupSearch();
  route();
}
boot();
