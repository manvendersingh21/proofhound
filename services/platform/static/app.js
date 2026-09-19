'use strict';
// ProofHound web app. Every value from the API is untrusted text (page titles, log lines,
// app-controlled strings): it is only ever assigned with textContent or SVG text nodes.

const VERDICTS = ['PASS', 'FAIL', 'INCONCLUSIVE', 'INFRA_ERROR'];
const VERDICT_LABEL = { PASS: '✓ Pass', FAIL: '✕ Fail', INCONCLUSIVE: '? Inconclusive', INFRA_ERROR: '! Infra error' };
const SVGNS = 'http://www.w3.org/2000/svg';
const view = document.getElementById('view');

// ---------------------------------------------------------------- tiny DOM helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null) continue;
    if (k === 'text') el.textContent = v; else if (k === 'tip') tipOn(el, v); else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid) el.append(kid);
  return el;
}
const fmtTime = iso => iso ? new Date(iso).toLocaleString() : '';
const short = id => id ? String(id).slice(0, 8) : '';
function badge(verdict, big) {
  return h('span', { class: `badge v-${verdict}${big ? ' big' : ''}` }, h('span', { class: 'dot', 'aria-hidden': 'true' }),
    VERDICT_LABEL[verdict] || verdict);
}
function card(title, ...kids) { return h('section', { class: 'card' }, title ? h('h2', { text: title }) : null, ...kids); }
function toast(msg) {
  const t = document.getElementById('toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 4000);
}
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'content-type': 'application/json' }, ...options,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined });
  const text = await res.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.detail) ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : `HTTP ${res.status}`);
  return data;
}
function busy(button, fn) {
  return async (...args) => {
    const label = button.textContent; button.disabled = true; button.textContent = 'Working…';
    try { await fn(...args); } catch (e) { toast(e.message); } finally { button.disabled = false; button.textContent = label; }
  };
}

// ---------------------------------------------------------------- tooltip
const tip = document.getElementById('tip');
function tipOn(el, text) {
  el.addEventListener('mouseenter', () => { tip.textContent = text; tip.hidden = false; });
  el.addEventListener('mousemove', e => {
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    const y = Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });
  el.addEventListener('mouseleave', () => { tip.hidden = true; });
}

// ---------------------------------------------------------------- theme
(function theme() {
  const saved = (() => { try { return localStorage.getItem('ph-theme'); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  document.getElementById('theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme === 'dark' ||
      (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('ph-theme', document.documentElement.dataset.theme); } catch { /* optional */ }
  });
})();

// ---------------------------------------------------------------- charts
function stackedVerdictBars(rows) {
  // Verdicts per day: stacked bars on one axis, status colors + legend + per-segment tooltip.
  if (!rows.length) return h('p', { class: 'empty', text: 'No runs yet.' });
  const W = 640, H = 220, L = 34, B = 26, T = 10;
  const max = Math.max(1, ...rows.map(r => VERDICTS.reduce((a, v) => a + r[v], 0)));
  const step = (W - L) / rows.length, bw = Math.min(40, step * 0.6);
  const y = v => T + (H - T - B) * (1 - v / max);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', 'aria-label': 'Run verdicts per day' });
  const ticks = [0, Math.ceil(max / 2), max];
  for (const t of ticks) svg.append(s('line', { class: 'gridline', x1: L, x2: W, y1: y(t), y2: y(t) }),
    s('text', { x: L - 6, y: y(t) + 4, 'text-anchor': 'end', text: String(t) }));
  rows.forEach((r, i) => {
    const x = L + i * step + (step - bw) / 2; let acc = 0;
    VERDICTS.forEach(v => {
      if (!r[v]) return;
      const y0 = y(acc), y1 = y(acc + r[v]); acc += r[v];
      svg.append(s('rect', { class: `f-${v} ring`, x, y: y1, width: bw, height: Math.max(1, y0 - y1), rx: 3,
        tip: `${r.day}\n${VERDICT_LABEL[v]}: ${r[v]} run(s)` }));
    });
    if (rows.length <= 14 || i % Math.ceil(rows.length / 10) === 0)
      svg.append(s('text', { x: x + bw / 2, y: H - 8, 'text-anchor': 'middle', text: r.day.slice(5) }));
  });
  svg.append(s('line', { class: 'baseline', x1: L, x2: W, y1: y(0), y2: y(0) }));
  const legend = h('div', { class: 'legend' }, VERDICTS.map(v => h('span', {}, h('i', { class: `l-${v}` }), VERDICT_LABEL[v])));
  return h('div', {}, legend, svg);
}

function hbars(entries, cls) {
  if (!entries.length) return h('p', { class: 'empty', text: 'Nothing recorded.' });
  const max = Math.max(...entries.map(e => e[1]));
  return h('div', {}, entries.map(([label, value, title]) => {
    const fill = h('div', { class: `fill ${cls}` }); fill.style.width = (100 * value / max) + '%';
    const row = h('div', { class: 'hbar' }, h('span', { text: label, title: title || label }), h('div', { class: 'track' }, fill),
      h('span', { class: 'mono', text: String(value) }));
    tipOn(row, `${title || label}: ${value}`); return row;
  }));
}

const LANES = [
  { key: 'network', label: 'Network', cls: 's1', match: e => e.source === 'network' },
  { key: 'server_log', label: 'Server log', cls: 's2', match: e => e.source === 'server_log' },
  { key: 'browser', label: 'Browser console', cls: 's4', match: e => e.source === 'console' || e.source === 'page_error' },
];

function evidenceTimeline(detail) {
  // X = event sequence (the order things were observed). One lane per evidence source; step
  // boundaries as dashed lines; failing steps shaded; error events ringed in red.
  const observed = detail.observed || {}; const steps = observed.steps || [];
  const events = (observed.events || []).filter(e => e.source !== 'runner');
  if (!steps.length) return h('p', { class: 'empty', text: 'No step evidence recorded.' });
  const spansByStep = {};
  for (const st of (detail.correlation || {}).steps || []) spansByStep[st.step] = st.backend_spans || [];
  const lanes = [...LANES, { key: 'spans', label: 'Backend spans', cls: 's3' }];
  const maxSeq = Math.max(1, ...steps.map(st => st.event_seq_end), ...events.map(e => e.seq));
  const W = 960, L = 118, laneH = 30, T = 44, H = T + lanes.length * laneH + 12;
  const x = seq => L + (W - L - 10) * (seq / maxSeq);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', 'aria-label': 'Evidence timeline by step' });
  steps.forEach(st => {
    const x0 = x(st.event_seq_start), x1 = Math.max(x(st.event_seq_end), x0 + 3);
    if (st.status !== 'PASS') svg.append(s('rect', { class: 'failband', x: x0, y: 4, width: x1 - x0, height: H - 8 }));
    svg.append(s('line', { class: 'stepline', x1: x0, x2: x0, y1: 4, y2: H - 6 }));
    svg.append(s('rect', { class: `f-${st.status} ring`, x: x0 + 1, y: 10, width: Math.max(2, x1 - x0 - 2), height: 12, rx: 3,
      tip: `Step ${st.step} (${st.action})\n${VERDICT_LABEL[st.status] || st.status}: ${st.reason}\n${st.elapsed_ms} ms` }));
    if (x1 - x0 > 46) svg.append(s('text', { class: 'ink', x: x0 + 4, y: 36, text: st.step.slice(0, Math.floor((x1 - x0) / 6.5)) }));
  });
  lanes.forEach((lane, i) => {
    const cy = T + i * laneH + laneH / 2;
    svg.append(s('text', { class: 'ink', x: 0, y: cy + 4, text: lane.label }),
      s('line', { class: 'gridline', x1: L, x2: W - 10, y1: cy, y2: cy }));
    if (lane.key === 'spans') {
      steps.forEach(st => (spansByStep[st.step] || []).forEach((span, j) => {
        const cx = x(st.event_seq_start) + 8 + j * 12;
        svg.append(s('rect', { class: `f-s3 ring`, x: cx - 5, y: cy - 5, width: 10, height: 10, rx: 2,
          tip: `Backend span: ${span.service}/${span.name}\n${span.duration_ms} ms · ${span.join} (${span.trace_basis})\nduring step ${st.step}` }));
      }));
      return;
    }
    events.filter(lane.match).forEach(e => {
      const cx = x(e.seq);
      svg.append(s('circle', { class: `f-${lane.cls} ring`, cx, cy, r: 5,
        tip: `${lane.label} · ${e.level}${e.status ? ' · HTTP ' + e.status : ''}\n${e.method || ''} ${e.path || ''}\n${e.message || ''}\n#${e.seq} ${fmtTime(e.at)}` }));
      if (e.level === 'error') svg.append(s('circle', { class: 'errring', cx, cy, r: 8 }));
    });
  });
  const legend = h('div', { class: 'legend' },
    lanes.map(l => h('span', {}, h('i', { class: `l-${l.cls}${l.key === 'spans' ? '' : ' round'}` }), l.label)),
    h('span', {}, h('i', { class: 'l-FAIL round' }), 'Red ring = error event'),
    h('span', {}, h('i', { class: 'l-FAIL' }), 'Shaded = step did not pass'));
  return h('div', {}, legend, svg);
}

function stepDurations(steps) {
  if (!steps.length) return null;
  const max = Math.max(1, ...steps.map(st => st.elapsed_ms));
  return h('div', {}, steps.map(st => {
    const fill = h('div', { class: `fill ${st.status === 'PASS' ? 's1' : 'crit'}` }); fill.style.width = (100 * st.elapsed_ms / max) + '%';
    const row = h('div', { class: 'hbar' }, h('span', {}, badge(st.status), ' ', st.step), h('div', { class: 'track' }, fill),
      h('span', { class: 'mono', text: Math.round(st.elapsed_ms) + 'ms' }));
    tipOn(row, `${st.step} (${st.action}): ${st.reason}`); return row;
  }));
}

// ---------------------------------------------------------------- views
const routes = [
  [/^\/?$/, overviewView, 'overview'], [/^\/sample$/, sampleView, 'sample'], [/^\/onboard$/, onboardView, 'onboard'],
  [/^\/projects$/, projectsView, 'projects'], [/^\/projects\/([\w-]+)$/, projectView, 'projects'],
  [/^\/runs$/, runsView, 'runs'], [/^\/runs\/([\w-]+)$/, runView, 'runs'], [/^\/logs$/, logsView, 'logs'],
];
let poll = null;
async function route() {
  clearInterval(poll); poll = null; tip.hidden = true;
  const path = (location.hash || '#/').slice(1).split('?')[0];
  for (const [re, fn, nav] of routes) {
    const m = path.match(re); if (!m) continue;
    document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === nav));
    view.replaceChildren(h('p', { class: 'muted', text: 'Loading…' }));
    try { await fn(...m.slice(1)); } catch (e) { view.replaceChildren(card('Something went wrong', h('p', { text: e.message }))); }
    view.focus(); return;
  }
  view.replaceChildren(card('Not found'));
}
window.addEventListener('hashchange', route);
document.addEventListener('DOMContentLoaded', route);

async function overviewView() {
  const [o, projects] = await Promise.all([api('/api/overview'), api('/api/projects')]);
  const k = o.kpis;
  const failingNow = o.scenarios.filter(sc => sc.latest === 'FAIL');
  const kpi = (label, value, sub) => h('div', { class: 'card kpi' }, h('div', { class: 'label', text: label }),
    h('div', { class: 'value', text: value }), h('div', { class: 'sub', text: sub }));
  const nodes = [h('h1', { text: 'How your app is doing' }),
    h('p', { class: 'lede', text: 'Every verdict comes from evidence: UI checks, browser and server logs, traces, and an independent state check. A UI success message alone never counts as a pass.' })];
  if (!k.runs) nodes.push(card('Get started', h('ol', { class: 'steps-guide' },
    h('li', {}, h('a', { href: '#/sample', text: 'Start the sample app' }), ' (or use your own authorized staging app).'),
    h('li', {}, h('a', { href: '#/onboard', text: 'Onboard it' }), ': ProofHound discovers pages, forms and links read-only.'),
    h('li', {}, h('a', { href: '#/projects', text: 'Review proposed workflows' }), ', approve the ones that change data and say how to verify them.'),
    h('li', {}, 'Run them, then open a run to see ', h('strong', { text: 'where' }), ' it went wrong on the evidence timeline.'),
    h('li', {}, 'Onboard the next version; ProofHound picks the regressions to re-run.'))));
  nodes.push(h('div', { class: 'kpis' },
    kpi('Runs', String(k.runs), `${k.pass} pass · ${k.fail} fail · ${k.inconclusive + k.infra_error} other`),
    kpi('Pass rate', k.pass_rate === null ? '—' : Math.round(k.pass_rate * 100) + '%', 'of runs with a PASS/FAIL verdict'),
    kpi('Failing now', String(failingNow.length), 'scenarios whose latest run failed'),
    kpi('Known issues', String(o.issues.length), 'grouped by fingerprint across runs')));
  const scen = o.scenarios.length ? h('table', {}, h('thead', {}, h('tr', {}, h('th', { text: 'Scenario' }), h('th', { text: 'Latest' }), h('th', { text: 'History (oldest → newest)' }))),
    h('tbody', {}, o.scenarios.slice(0, 15).map(sc => {
      const latest = sc.history[sc.history.length - 1];
      return h('tr', { class: 'clickable', onclick: () => { location.hash = '#/runs/' + latest.run_id; } },
        h('td', {}, h('div', { text: sc.scenario_id }), h('div', { class: 'muted', text: sc.project_id })),
        h('td', {}, badge(sc.latest)),
        h('td', {}, h('div', { class: 'dots' }, sc.history.map(r => {
          const a = h('a', { class: r.verdict, href: '#/runs/' + r.run_id, 'aria-label': `${VERDICT_LABEL[r.verdict]} ${fmtTime(r.created_at)}` });
          tipOn(a, `${VERDICT_LABEL[r.verdict]}\n${fmtTime(r.created_at)}`); return a;
        }))));
    }))) : h('p', { class: 'empty', text: 'No scenarios run yet.' });
  const issues = o.issues.length ? h('ul', { class: 'clean' }, o.issues.map(i => h('li', {},
    h('div', {}, h('strong', { text: i.category.replaceAll('_', ' ') }), ' at step ', h('code', { text: i.step })),
    h('div', { class: 'muted', text: `${i.reason} · seen ${i.occurrences}× · last ${fmtTime(i.last_seen)}` }),
    h('a', { href: '#/runs/' + i.latest_run_id, text: 'Open latest run →' })))) : h('p', { class: 'empty', text: 'No failures recorded.' });
  nodes.push(h('div', { class: 'grid cols-3' },
    card('Verdicts per day', stackedVerdictBars(o.by_day)),
    card('Where errors come from', h('p', { class: 'muted', text: `Error-level events, ${o.error_events_scope}` }),
      hbars(Object.entries(o.error_events_by_source).map(([k2, v]) => [k2.replace('_', ' '), v]), 'crit'))));
  nodes.push(h('div', { class: 'grid cols-2' }, card('Scenario health', scen), card("What's going wrong", issues)));
  if (projects.length) nodes.push(card('Onboarded apps', h('ul', { class: 'clean' }, projects.map(p => h('li', {},
    h('a', { href: '#/projects/' + p.project_id, text: p.project_id }), ` · ${p.version} · ${p.summary.pages} pages · ${p.summary.workflows} workflows · ${p.approved} approved`)))));
  view.replaceChildren(...nodes);
}

async function sampleView() {
  const status = await api('/api/sample-app');
  const start = (release, broken) => { const b = h('button', { type: 'button', class: broken ? '' : 'ghost',
    text: `Start ${release}${broken ? ' (with defect)' : ' (fixed, /loans changed)'}` });
    b.addEventListener('click', busy(b, async () => { await api('/api/sample-app', { method: 'POST', body: { release, broken } }); route(); })); return b; };
  const stop = h('button', { type: 'button', class: 'ghost', text: 'Stop' });
  stop.addEventListener('click', busy(stop, async () => { await api('/api/sample-app', { method: 'DELETE' }); route(); }));
  const info = status.origin ? h('div', { class: 'stack' },
    h('table', {}, h('tbody', {}, [['App URL', status.origin], ['State oracle (read-only)', status.oracle],
      ['Auth token env var', status.auth_env], ['Server log', status.log_file || '(log tailing disabled)'],
      ['Release', `${status.release}${status.broken ? ' · contains the lost-write defect' : ' · fixed'}`]]
      .map(([a, b]) => h('tr', {}, h('th', { text: a }), h('td', { class: 'mono', text: b }))))),
    h('div', { class: 'actions' }, h('a', { class: 'button', href: '#/onboard', text: 'Onboard this app →' }), stop)) :
    h('p', { class: 'muted', text: 'Not running.' });
  view.replaceChildren(h('h1', { text: 'Sample app' }),
    h('p', { class: 'lede', text: 'A small lending-library app to try every step safely. It needs a bearer token, has a destructive “Delete account” link the crawler must not follow, and release v2 says “Reserved” for the last copy without saving it.' }),
    card('Status', info), card('Start', h('div', { class: 'actions' }, start('v2', true), start('v3', false))));
}

async function onboardView() {
  const sample = await api('/api/sample-app');
  const form = h('form', { class: 'stack' },
    h('div', { class: 'row' },
      h('label', {}, 'App URL (authorized staging origin)', h('input', { name: 'origin', required: true, value: sample.origin || '', placeholder: 'http://127.0.0.1:8765' })),
      h('label', {}, 'Project', h('input', { name: 'project_id', value: 'library', pattern: '[A-Za-z0-9_\\-]+', required: true })),
      h('label', {}, 'Version', h('input', { name: 'version', value: sample.release || 'v1', pattern: '[A-Za-z0-9_\\-]+', required: true }))),
    h('div', { class: 'row' },
      h('label', {}, 'Auth token env var (name only; never the token)', h('input', { name: 'auth_env', value: sample.auth_env || '', placeholder: 'QA_TARGET_TOKEN_MYAPP' })),
      h('label', {}, 'Max pages', h('input', { name: 'max_pages', type: 'number', min: 1, max: 30, value: 10 }))),
    h('p', { class: 'muted', text: 'Read-only: follows same-origin links, never clicks or submits, skips links that look destructive.' }),
    h('div', { class: 'actions' }, h('button', { type: 'submit', text: 'Discover' })));
  const out = h('div', { class: 'stack' });
  form.addEventListener('submit', async ev => {
    ev.preventDefault(); const b = form.querySelector('button');
    await busy(b, async () => {
      const f = new FormData(form);
      const body = { origin: f.get('origin'), project_id: f.get('project_id'), version: f.get('version'),
        max_pages: Number(f.get('max_pages')), auth_env: f.get('auth_env') || null };
      const r = await api('/api/onboard', { method: 'POST', body });
      out.replaceChildren(...onboardingResult(r));
    })();
  });
  view.replaceChildren(h('h1', { text: '1 · Onboard an app' }),
    h('p', { class: 'lede', text: 'Point ProofHound at an app you are authorized to test. It maps pages, forms and controls, and proposes workflows. Everything it observed is separated from what it merely infers.' }),
    card('Target', form), out);
}

function onboardingResult(r) {
  const nodes = [card(`Discovered ${r.summary.pages} pages · ${r.summary.forms} forms · ${r.summary.workflows} workflows`,
    h('div', { class: 'actions' }, h('a', { class: 'button', href: '#/projects/' + r.project_id, text: 'Review & approve workflows →' })),
    r.skipped_unsafe_links.length ? h('div', { class: 'callout' }, 'Not followed (looks destructive): ', r.skipped_unsafe_links.join(', ')) : null,
    h('table', {}, h('thead', {}, h('tr', {}, ['Page', 'HTTP', 'Title', 'Forms', 'Links to'].map(t => h('th', { text: t })))),
      h('tbody', {}, r.pages.map(p => h('tr', {}, h('td', { class: 'mono', text: p.path }), h('td', { text: String(p.http_status ?? p.status ?? '') }),
        h('td', { text: p.title || '' }), h('td', { text: String((p.forms || []).length) }), h('td', { class: 'mono', text: (p.discovered_links || []).join(' ') }))))))];
  if (r.changes) nodes.push(changesCard(r.changes));
  return nodes;
}

function changesCard(c) {
  return card(`What changed since ${c.from_version}`, h('div', { class: 'split' },
    h('div', {}, h('h3', { text: 'Changed / added pages' }), h('p', { class: 'mono', text: [...c.pages_changed, ...c.pages_added].join(', ') || 'none' })),
    h('div', {}, h('h3', { text: 'Removed pages' }), h('p', { class: 'mono', text: c.pages_removed.join(', ') || 'none' }))),
    h('p', { class: 'muted', text: 'Detected from UI structure only; backend-only changes are invisible to this signal.' }));
}

async function projectsView() {
  const list = await api('/api/projects');
  view.replaceChildren(h('h1', { text: '2 · Workflows & approvals' }),
    list.length ? card(null, h('table', {}, h('thead', {}, h('tr', {}, ['App', 'Version', 'Origin', 'Pages', 'Workflows', 'Approved'].map(t => h('th', { text: t })))),
      h('tbody', {}, list.map(p => h('tr', { class: 'clickable', onclick: () => { location.hash = '#/projects/' + p.project_id; } },
        h('td', { text: p.project_id }), h('td', { text: p.version }), h('td', { class: 'mono', text: p.origin }),
        h('td', { class: 'num', text: String(p.summary.pages) }), h('td', { class: 'num', text: String(p.summary.workflows) }),
        h('td', { class: 'num', text: String(p.approved) })))))) :
      card(null, h('p', { class: 'empty' }, 'No apps onboarded yet. ', h('a', { href: '#/onboard', text: 'Onboard one →' }))));
}

async function projectView(project) {
  const [p, sample] = await Promise.all([api('/api/projects/' + project), api('/api/sample-app')]);
  const rows = p.workflows.map(w => {
    const actions = h('div', { class: 'actions' });
    if (w.status === 'NOT_GENERATED') actions.append(h('span', { class: 'muted', text: 'Not generated: ' + w.reason }));
    else {
      if (!w.mutating || w.approved) {
        const run = h('button', { type: 'button', class: 'small', text: 'Run' });
        run.addEventListener('click', busy(run, async () => {
          const job = await api('/api/runs', { method: 'POST', body: { project_id: project, workflow_id: w.id } });
          toast(`Queued ${w.id}`); location.hash = '#/runs?job=' + job.job_id;
        }));
        actions.append(run);
      }
      if (w.mutating) {
        const ap = h('button', { type: 'button', class: 'small ghost', text: w.approved ? 'Edit approval' : 'Approve…' });
        ap.addEventListener('click', () => approvalDialog(p, w, sample, holder)); actions.append(ap);
      }
    }
    return h('tr', {},
      h('td', {}, h('div', { class: 'mono', text: w.id }), h('div', { class: 'muted', text: `${w.kind.replace('_', ' ')} · ${w.path}` })),
      h('td', {}, w.mutating ? h('span', { class: 'chip', text: w.approved ? 'changes data · approved' : 'changes data · needs approval' }) : h('span', { class: 'chip', text: 'read-only' })),
      h('td', {}, w.discovered ? h('div', { class: 'muted', text: w.discovered.submit ? `Form: ${(w.discovered.fields || []).map(f => f.label || f.tag).join(', ')} → “${w.discovered.submit}”` : `Title: ${w.discovered.title || ''}` }) : null,
        w.inferred ? h('div', {}, h('em', { text: 'Hypothesis: ' }), w.inferred.hypothesis) : null),
      h('td', {}, w.last_verdict ? badge(w.last_verdict) : h('span', { class: 'muted', text: 'never run' })),
      h('td', {}, actions));
  });
  const holder = h('div');
  const nodes = [h('h1', { text: `${p.project_id} · ${p.version}` }), h('p', { class: 'lede' }, 'Origin ', h('code', { text: p.origin }),
    '. Read-only workflows can run now. Workflows that change data need your approval, including how to verify the result independently.'), holder];
  if (p.regression_selection) {
    const go = h('button', { type: 'button', text: `Run ${p.regression_selection.selected.length} selected regression(s)` });
    go.addEventListener('click', busy(go, async () => {
      const r = await api(`/api/projects/${project}/regressions`, { method: 'POST' });
      toast(`Started ${r.started.length}, skipped ${r.skipped.length}` + (r.skipped.length ? ': ' + r.skipped.map(x => `${x.id} (${x.reason})`).join('; ') : ''));
      location.hash = '#/runs';
    }));
    nodes.push(card('Regression selection', p.changes ? changesCard(p.changes) : null,
      h('ul', { class: 'clean' }, p.regression_selection.selected.map(x => h('li', {}, h('code', { text: x.id }), ' — ', x.reasons.join('; ')))),
      h('p', { class: 'muted', text: p.regression_selection.note }), h('div', { class: 'actions' }, go)));
  }
  nodes.push(h('section', { class: 'card flush' }, h('table', {}, h('thead', {}, h('tr', {}, ['Workflow', 'Kind', 'Discovered / inferred', 'Last result', ''].map(t => h('th', { text: t })))), h('tbody', {}, rows))));
  view.replaceChildren(...nodes);
}

function approvalDialog(p, w, sample, holder) {
  const prev = p.approvals[w.id] || {};
  const checks = h('div', { class: 'stack' });
  const addCheck = (c = {}) => {
    const row = h('div', { class: 'row' },
      h('label', {}, 'State path', h('input', { name: 'oracle_path', value: c.oracle_path || '/state' })),
      h('label', {}, 'JSON pointer', h('input', { name: 'pointer', value: c.pointer || '', placeholder: '/reservations' })),
      h('label', {}, 'Operator', h('select', { name: 'operator' }, ['equals', 'not_equals', 'contains', 'exists', 'length_equals', 'less_or_equal']
        .map(o => h('option', { value: o, selected: (c.operator || 'equals') === o }, o)))),
      h('label', {}, 'Expected (JSON)', h('input', { name: 'expected', value: c.expected === undefined ? '' : JSON.stringify(c.expected), placeholder: '1' })));
    checks.append(row);
  };
  (prev.oracle_checks || [{}]).forEach(addCheck);
  const more = h('button', { type: 'button', class: 'ghost small', text: '+ Add state check', onclick: () => addCheck() });
  const uiCheck = (prev.ui_checks || [])[0];
  const form = h('form', { class: 'stack' },
    h('label', {}, 'Independent state service (read-only, GET)', h('input', { name: 'oracle_origin', required: true, value: prev.oracle_origin || sample.oracle || '' })),
    h('h3', { text: 'After the workflow, the real state must satisfy' }), checks, more,
    h('h3', { text: 'Optional: what the UI should claim (recorded, never sufficient on its own)' }),
    h('div', { class: 'row' }, h('label', {}, 'Element id', h('input', { name: 'ui_id', value: uiCheck ? uiCheck.locator.value : '' , placeholder: 'status' })),
      h('label', {}, 'Expected text', h('input', { name: 'ui_text', value: uiCheck ? uiCheck.expected : '', placeholder: 'Reserved' }))),
    h('label', { class: 'check' }, h('input', { type: 'checkbox', name: 'use_log', checked: !!(prev.log_file || sample.log_file) }), 'Tail the server log during the run'),
    h('label', {}, 'Server log file', h('input', { name: 'log_file', value: prev.log_file || sample.log_file || '' })),
    h('label', { class: 'check' }, h('input', { type: 'checkbox', name: 'consent', required: true }),
      'I am authorized to let ProofHound submit this form against this environment (it changes data).'),
    h('div', { class: 'actions' }, h('button', { type: 'submit', text: 'Approve workflow' }), h('button', { type: 'button', class: 'ghost', text: 'Cancel', onclick: () => holder.replaceChildren() })));
  form.addEventListener('submit', async ev => {
    ev.preventDefault(); const f = new FormData(form); const b = form.querySelector('button[type=submit]');
    await busy(b, async () => {
      const oracle_checks = [...checks.children].map(row => {
        const get = n => row.querySelector(`[name=${n}]`).value; let expected = get('expected');
        try { expected = JSON.parse(expected); } catch { /* keep as string */ }
        return { oracle_path: get('oracle_path'), pointer: get('pointer'), operator: get('operator'), expected };
      }).filter(c => c.pointer);
      const approval = { allow_mutations: true, oracle_origin: f.get('oracle_origin'), oracle_checks, propagate_trace: true };
      if (f.get('ui_id') && f.get('ui_text')) approval.ui_checks = [{ name: 'ui_claim', action: 'assert_text', locator: { by: 'id', value: f.get('ui_id') }, expected: f.get('ui_text') }];
      if (f.get('use_log') && f.get('log_file')) { approval.log_file = f.get('log_file'); approval.log_messages = true; }
      await api('/api/approve', { method: 'POST', body: { project_id: p.project_id, workflow_id: w.id, approval } });
      toast('Approved ' + w.id); route();
    })();
  });
  holder.replaceChildren(card(`Approve ${w.id}`, h('p', { class: 'muted', text: 'ProofHound never guesses what “correct” means. Tell it how to check the real outcome.' }), form));
  holder.scrollIntoView({ behavior: 'smooth' });
}

async function runsView() {
  const jobId = new URLSearchParams(location.hash.split('?')[1] || '').get('job');
  const draw = async () => {
    const [runs, jobs] = await Promise.all([api('/api/runs?limit=100'), api('/api/jobs')]);
    const active = jobs.filter(j => j.status === 'queued' || j.status === 'running');
    const recentJobs = jobs.slice(0, 8);
    const jobCard = recentJobs.length ? card('Runs in progress', h('div', { class: 'jobs' }, recentJobs.map(j => h('div', {},
      h('span', { class: 'chip', text: j.status }), ' ', h('code', { text: j.scenario_id }), ' ', h('span', { class: 'muted', text: j.label }),
      j.run_id ? h('span', {}, ' → ', badge(j.verdict), ' ', h('a', { href: '#/runs/' + j.run_id, text: 'open' })) : null,
      j.error ? h('span', { class: 'muted', text: ' ' + j.error }) : null)))) : null;
    view.replaceChildren(h('h1', { text: '3 · Runs' }), jobCard || '',
      runs.length ? h('section', { class: 'card flush' }, h('div', { class: 'scroll' }, h('table', {},
        h('thead', {}, h('tr', {}, ['When', 'App', 'Scenario', 'Version', 'Verdict', 'Findings', 'Run'].map(t => h('th', { text: t })))),
        h('tbody', {}, runs.map(r => h('tr', { class: 'clickable', onclick: () => { location.hash = '#/runs/' + r.run_id; } },
          h('td', { text: fmtTime(r.created_at) }), h('td', { text: r.project_id }), h('td', { class: 'mono', text: r.scenario_id }),
          h('td', { text: r.version }), h('td', {}, badge(r.verdict)), h('td', { class: 'num', text: String((r.findings || []).length) }),
          h('td', { class: 'mono', text: short(r.run_id) }))))))) : card(null, h('p', { class: 'empty', text: 'No runs yet.' })));
    if (jobId) { const j = jobs.find(x => x.job_id === jobId); if (j && j.run_id && j.status === 'done') { location.hash = '#/runs/' + j.run_id; return; } }
    if (!active.length && poll) { clearInterval(poll); poll = null; }
  };
  await draw();
  poll = setInterval(() => draw().catch(() => {}), 1500);
}

async function runView(runId) {
  const d = await api('/api/runs/' + runId);
  const r = d.record, o = d.observed || {};
  const failed = (r.assertions || []).filter(a => a.status === 'FAIL');
  const claims = (r.assertions || []).filter(a => a.source === 'dom' && a.status === 'PASS');
  const oracleFail = failed.some(a => a.source === 'oracle');
  const wrong = [];
  if (r.verdict === 'PASS') wrong.push(h('p', { text: 'All checks passed, including the independent ones.' }));
  if (r.error) wrong.push(h('p', { text: r.error }));
  if (oracleFail && claims.length) wrong.push(h('div', { class: 'callout' }, h('strong', { text: 'The UI said it worked, the real state says it did not. ' }),
    `UI checks that passed: ${claims.map(c => c.step).join(', ')}. Independent state checks that failed: ${failed.filter(a => a.source === 'oracle').map(a => a.step).join(', ')}.`));
  if (failed.length) wrong.push(h('h3', { text: 'Verified failures' }), h('ul', { class: 'clean' }, failed.map(a => h('li', {},
    badge('FAIL'), ' ', h('code', { text: a.step }), ` (${a.source}) — ${a.reason}`))));
  const hyps = (r.findings || []).map(f => h('li', {}, h('strong', { text: f.category.replaceAll('_', ' ') }), ` at ${f.step}`,
    f.correlated_sources && f.correlated_sources.length ? ` · nearby errors from ${f.correlated_sources.join(', ')}` : '',
    h('span', { class: 'muted', text: ' · root cause not confirmed' })));
  for (const st of (d.correlation || {}).steps || []) {
    const names = (st.backend_spans || []).map(b => b.name);
    if (names.some(n => n.startsWith('POST')) && !names.some(n => n.startsWith('db.')))
      hyps.push(h('li', {}, `Step ${st.step}: the backend handled the request but no database-write span followed. Consistent with an acknowledged-but-uncommitted write; not proof.`));
  }
  if (hyps.length) wrong.push(h('h3', { text: 'Hypotheses (unconfirmed)' }), h('ul', { class: 'clean' }, hyps));
  const limits = ((d.correlation || {}).limits || []).map(l => h('li', { class: 'muted', text: l }));
  const perStep = ((d.correlation || {}).steps || []).filter(st => st.network.length || st.server_logs.length || st.backend_spans.length || st.other_events.length)
    .map(st => h('tr', {}, h('td', {}, badge(st.status), ' ', h('code', { text: st.step })),
      h('td', {}, st.network.map(n => h('span', { class: 'chip', text: `${n.method || ''} ${n.path || ''} → ${n.status ?? '?'}` }))),
      h('td', {}, st.server_logs.map(l => h('span', { class: 'chip' + (l.join === 'trace_linked' ? ' linked' : ''), text: `${l.level}${l.join === 'trace_linked' ? ' · trace-linked' : ''}` }))),
      h('td', {}, st.backend_spans.map(b => h('span', { class: 'chip linked', text: `${b.service}/${b.name} ${b.duration_ms}ms` }))),
      h('td', {}, st.other_events.map(e => h('span', { class: 'chip', text: `${e.source} ${e.level}` })))));
  const nodes = [
    h('p', {}, h('a', { href: '#/runs', text: '← All runs' })),
    h('div', { class: 'actions' }, h('h1', { text: r.scenario_id }), badge(r.verdict, true)),
    h('p', { class: 'lede' }, `${r.project_id} · ${r.version} · ${fmtTime(r.created_at)} · trace `, h('code', { text: r.trace_id }), ' · run ', h('code', { text: r.run_id })),
    h('section', { class: `card ${r.verdict === 'PASS' ? 'panel-good' : r.verdict === 'FAIL' ? 'panel-bad' : 'panel-warn'}` },
      h('h2', { text: r.verdict === 'PASS' ? 'Everything checked out' : 'What went wrong' }), ...wrong),
    card('Where it went wrong: evidence timeline', h('p', { class: 'muted', text: 'Left to right in the order things were observed. Hover any mark for details.' }), evidenceTimeline(d),
      limits.length ? h('ul', { class: 'clean' }, limits) : null),
    h('div', { class: 'grid cols-2' }, card('Step durations', stepDurations(o.steps || []) || h('p', { class: 'empty', text: 'No steps.' })),
      card('Evidence per step', perStep.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['Step', 'Network', 'Server log', 'Backend spans', 'Browser'].map(t => h('th', { text: t })))), h('tbody', {}, perStep))) : h('p', { class: 'empty', text: 'No correlated evidence.' }))),
  ];
  if (d.bug_report) {
    const copy = h('button', { type: 'button', class: 'ghost small', text: 'Copy bug report' });
    copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(d.bug_report); toast('Copied'); } catch { toast('Copy not available'); } });
    nodes.push(card('Reproducible bug report', h('div', { class: 'actions' }, copy), h('pre', { class: 'block', text: d.bug_report })));
  }
  nodes.push(card('Raw evidence', h('div', { class: 'actions' },
    h('a', { class: 'button ghost', href: `/ui/runs/${r.run_id}/report`, target: '_blank', rel: 'noopener', text: 'HTML report' }),
    h('a', { class: 'button ghost', href: `/engine/evidence/${r.evidence_sha256}`, target: '_blank', rel: 'noopener', text: 'Evidence JSON' }),
    h('a', { class: 'button ghost', href: `#/logs?run_id=${r.run_id}`, text: 'Logs for this run' })),
    h('p', { class: 'muted' }, 'Evidence is content-addressed: sha256 ', h('code', { text: r.evidence_sha256 }))));
  view.replaceChildren(...nodes);
}

async function logsView() {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const form = h('form', { class: 'row' },
    h('label', {}, 'Source', h('select', { name: 'source' }, ['', 'network', 'server_log', 'console', 'page_error', 'observer', 'runner']
      .map(v => h('option', { value: v, selected: params.get('source') === v }, v === '' ? 'All app evidence' : v === 'runner' ? 'runner (step bookkeeping)' : v)))),
    h('label', {}, 'Level', h('select', { name: 'level' }, ['', 'error', 'warning', 'info'].map(v => h('option', { value: v, selected: params.get('level') === v }, v || 'All')))),
    h('label', {}, 'Run id', h('input', { name: 'run_id', value: params.get('run_id') || '', placeholder: 'any' })),
    h('label', {}, ' ', h('button', { type: 'submit', text: 'Filter' })));
  form.addEventListener('submit', ev => { ev.preventDefault(); const f = new URLSearchParams();
    for (const [k, v] of new FormData(form)) if (v) f.set(k, v); location.hash = '#/logs?' + f.toString(); });
  const q = new URLSearchParams(); for (const k of ['source', 'level', 'run_id']) if (params.get(k)) q.set(k, params.get(k));
  const raw = await api('/api/logs?' + q.toString());
  // Runner events only mark step boundaries; hide them unless asked for explicitly.
  const data = { ...raw, events: params.get('source') === 'runner' ? raw.events : raw.events.filter(e => e.source !== 'runner') };
  const bySource = {};
  for (const e of data.events) { const b = bySource[e.source] ||= { error: 0, other: 0 }; b[e.level === 'error' ? 'error' : 'other'] += 1; }
  const chart = hbars(Object.entries(bySource).sort((a, b) => b[1].error - a[1].error).map(([k2, v]) => [k2.replace('_', ' '), v.error, `${k2}: ${v.error} error, ${v.other} other`]).filter(x => x[1] > 0), 'crit');
  const table = data.events.length ? h('section', { class: 'card flush' }, h('div', { class: 'scroll' }, h('table', {},
    h('thead', {}, h('tr', {}, ['Time', 'Level', 'Source', 'Detail', 'Step', 'Run'].map(t => h('th', { text: t })))),
    h('tbody', {}, data.events.map(e => h('tr', {},
      h('td', { class: 'mono time', text: new Date(e.at).toLocaleTimeString() }),
      h('td', {}, h('span', { class: `badge v-${e.level === 'error' ? 'ERROR' : e.level === 'warning' ? 'WARN' : 'INFO'}` }, h('span', { class: 'dot' }), e.level)),
      h('td', { text: e.source }),
      h('td', { class: 'mono', text: [e.method, e.path, e.status ? '→ ' + e.status : '', e.message].filter(Boolean).join(' ') || (e.source === 'runner' ? 'step started' : '(message text not retained for this run)') }),
      h('td', { class: 'mono', text: e.step || '' }),
      h('td', {}, h('a', { href: '#/runs/' + e.run_id, text: short(e.run_id) }), ' ', badge(e.verdict)))))))) : card(null, h('p', { class: 'empty', text: 'No matching events.' }));
  view.replaceChildren(h('h1', { text: '4 · Logs' }),
    h('p', { class: 'lede', text: `Browser, network, server-log and runner events from the last ${data.runs_scanned} runs, newest first. ${data.note}.` }),
    card(null, form), h('div', { class: 'grid cols-3' }, table, card('Errors by source', chart)));
}
