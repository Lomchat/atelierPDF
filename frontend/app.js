/* ==========================================================================
   Atelier PDF — client
   No dependency, no build step. State lives here, truth lives on the server:
   the client only exchanges match identifiers.

   Two network paths, two costs:
     /scan     redefines the list of matches  -> when the RULES change
     /simulate computes the result without writing -> when a SETTING changes
   A 300-page scan costs ~1 s: we never fire one for a checkbox.
   ========================================================================== */

'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

/* Rule identifiers are never reused, even after a deletion: an in-flight
   response can therefore not land on the wrong row. */
let RULE_SEQ = 0;
const newRule = () => ({
  id: 'f' + (++RULE_SEQ),
  kind: 'text', query: '', replace: '',
  case_sensitive: false, whole_word: false, loose: true, enabled: true,
});

/* A batch holds N documents to which the same rules apply. */
function newDoc(x) {
  return {
    id: x.id, filename: x.filename, size: x.size, pages: x.pages || 0,
    error: x.error || null,
    analysis: x.analysis || null,
    occ: (x.analysis && x.analysis.occurrences) || [],
    sim: {},                 // match identifier -> result
    outcome: null,
    has_output: !!x.has_output,
    /* Fine settings, indexed by match SIGNATURE and not by identifier: the
       identifiers (p0-1, p0-2…) are handed out in document order and shift as
       soon as a rule changes. The signature does not move. */
    overrides: {},           // signature -> forced text
    fits: {},                // signature -> auto | exact | fit | push
    unchecked: new Set(),    // signatures explicitly unchecked
    checked: new Set(),      // checked identifiers (derived)
  };
}

const S = {
  sid: null,
  docs: [],                // every document in the batch
  did: null,               // the one being looked at

  finders: [],             // "find -> replace" rules
  dates: false,            // is the date finder switched on
  dateMode: 'map',         // map | shift | manual
  mapTargets: {},          // date key -> YYYY-MM-DD
  shift: { days: 0, months: 0, years: 1 },
  anchors: {},

  page: 0, view: 'before', wipe: 55, dpi: 0,
  selected: null,
  busy: false,
};

const D = () => S.docs.find(d => d.id === S.did) || null;

/* The current document is exposed under the same names as before: all the
   display code keeps talking about "matches" and "simulation" without knowing
   there is now one set of them per document. */
for (const [name, def] of Object.entries({
  analysis: null, occ: [], sim: {}, outcome: null,
  checked: new Set(), overrides: {}, fits: {}, unchecked: new Set(),
  filename: '', size: 0,
})) {
  Object.defineProperty(S, name, {
    get() { const d = D(); return d ? d[name] : def; },
    set(v) { const d = D(); if (d) d[name] = v; },
  });
}

/* Aggregates over the whole batch — this is what the rule counters, the bottom
   summary and the report look at. */
function lotDocs() { return S.docs.filter(d => !d.error); }

function lotTotals() {
  const t = { docs: 0, hit: 0, occ: 0, checked: 0, byFinder: {}, errors: {} };
  for (const d of lotDocs()) {
    t.docs++;
    if (d.occ.length) t.hit++;
    t.occ += d.occ.length;
    t.checked += d.checked.size;
    const c = (d.analysis && d.analysis.finder_counts) || {};
    for (const k in c) t.byFinder[k] = (t.byFinder[k] || 0) + c[k];
    const e = (d.analysis && d.analysis.finder_errors) || {};
    for (const k in e) t.errors[k] = e[k];
  }
  return t;
}

const MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];

const FIT_LABELS = { auto: 'auto', exact: 'exact', fit: 'fitted', push: 'pushed' };

/* ================================================================ saved rules
   Saved-rule shortcuts are kept in this browser. The server also records
   submitted rules in the administrator operation log.
   ================================================================ */

const MEM_KEY = 'atelierpdf.rules.v1';
const MEM_MAX = 40;

/* Two rules are "the same" if they find and replace alike, with the same
   options. The row identifier (f1, f2…) does not count: it belongs to a single
   session. */
const memKey = r => [r.kind || 'text', r.case_sensitive ? 1 : 0,
                     r.whole_word ? 1 : 0, r.query, r.replace].join('\u0001');

function memLoad() {
  try {
    const a = JSON.parse(localStorage.getItem(MEM_KEY) || '[]');
    return Array.isArray(a) ? a.filter(r => r && typeof r.query === 'string') : [];
  } catch (_) { return []; }
}

function memWrite(list) {
  try { localStorage.setItem(MEM_KEY, JSON.stringify(list.slice(0, MEM_MAX))); }
  catch (_) { /* private browsing, quota: saving rules is a comfort */ }
}

/* Called after a successful apply: the only moment when we know the rule was
   worth keeping. */
function memRemember(finders) {
  const list = memLoad();
  const byKey = new Map(list.map(r => [memKey(r), r]));
  let touched = false;

  for (const f of finders) {
    if (f.kind === 'dates' || !f.query.trim()) continue;
    const entry = {
      kind: f.kind, query: f.query, replace: f.replace,
      case_sensitive: !!f.case_sensitive, whole_word: !!f.whole_word,
    };
    const k = memKey(entry);
    const prev = byKey.get(k);
    entry.n = (prev && prev.n || 0) + 1;
    entry.at = Date.now();
    byKey.set(k, entry);
    touched = true;
  }
  if (!touched) return;

  const out = [...byKey.values()].sort((a, b) => (b.at || 0) - (a.at || 0));
  memWrite(out);
  renderMemory();
}

function memForget(key) {
  memWrite(memLoad().filter(r => memKey(r) !== key));
  renderMemory();
}

/* A chip is "on" when the rule appears as is in the current list. Clicking
   toggles: it is added, or taken out. */
function memActiveKeys() {
  return new Set(S.finders.filter(f => f.query.trim()).map(memKey));
}

function memToggle(key) {
  const active = memActiveKeys();
  if (active.has(key)) {
    S.finders = S.finders.filter(f => !(f.query.trim() && memKey(f) === key));
    if (!S.finders.length) S.finders.push(newRule());
  } else {
    const saved = memLoad().find(r => memKey(r) === key);
    if (!saved) return;
    // Reuse an empty row rather than leaving one lying around.
    const blank = S.finders.find(f => !f.query.trim() && !f.replace.trim());
    const target = blank || (S.finders.push(newRule()), S.finders[S.finders.length - 1]);
    Object.assign(target, {
      kind: saved.kind || 'text', query: saved.query, replace: saved.replace,
      case_sensitive: !!saved.case_sensitive, whole_word: !!saved.whole_word,
    });
  }
  renderRuleList();
  renderMemory();
  scheduleScan();
}

function renderMemory() {
  const list = memLoad();
  const card = $('#memCard'), el = $('#memList');
  if (!card || !el) return;
  card.hidden = !list.length;
  if (!list.length) { el.innerHTML = ''; return; }

  const active = memActiveKeys();
  el.innerHTML = list.map(r => {
    const k = memKey(r);
    const on = active.has(k);
    const opts = [r.kind === 'regex' ? '.*' : '', r.case_sensitive ? 'Aa' : '',
                  r.whole_word ? 'word' : ''].filter(Boolean);
    const title = `${r.query} → ${r.replace || '(empty)'}` +
      (r.n > 1 ? ` · used ${r.n} times` : '');
    return `<span class="mem${on ? ' on' : ''}" data-k="${esc(k)}">
      <button class="mem-label" data-act="toggle" data-k="${esc(k)}"
              aria-pressed="${on}" title="${esc(title)}">
        <span class="mem-q">${esc(r.query)}</span>
        <span class="mem-arrow">→</span>
        <span class="mem-r">${esc(r.replace || '∅')}</span>
        ${opts.map(o => `<span class="mem-opt">${esc(o)}</span>`).join('')}
      </button>
      <button class="mem-del" data-act="forget" data-k="${esc(k)}"
              title="forget this rule" aria-label="forget">✕</button>
    </span>`;
  }).join('');
}

/* Rule text changes on every keystroke: we refresh the active state without
   rebuilding the chips. */
function refreshMemoryState() {
  const active = memActiveKeys();
  $$('#memList .mem').forEach(node => {
    const on = active.has(node.dataset.k);
    node.classList.toggle('on', on);
    const b = node.querySelector('.mem-label');
    if (b) b.setAttribute('aria-pressed', String(on));
  });
}

/* ---------------------------------------------------------------- helpers */

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

const cssEsc = (window.CSS && CSS.escape)
  ? s => CSS.escape(s)
  : s => String(s).replace(/["\\]/g, '\\$&');

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 3200);
}

function debounce(fn, ms) {
  let h;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

async function api(path, opts = {}) {
  const headers = new Headers(opts.headers);
  headers.set('X-Atelier-Source', 'plateforme');
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

const jsonPost = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const fmtBytes = n => n < 1024 ? n + ' B'
  : n < 1048576 ? (n / 1024).toFixed(0) + ' kB'
  : (n / 1048576).toFixed(1) + ' MB';

const plural = (n, s = 's') => n > 1 ? s : '';

/* Never DD/MM or MM/DD here: this label sits right next to a native date
   input, and the two conventions read as different days. A spelled-out month
   cannot be misread. */
function prettyKey(key) {
  const p = String(key || '').split('-');
  if (p.length === 3) return `${+p[2]} ${(MONTHS[+p[1] - 1] || '').slice(0, 3)} ${p[0]}`;
  if (p.length === 2) return `${MONTHS[+p[1] - 1] || p[1]} ${p[0]}`;
  return key;
}

/* Rewriting the DOM under the user's fingers steals the caret. Every field
   carries a stable data-fkey; we find it again after the render. */
function keepFocus(root, render) {
  const ae = document.activeElement;
  const key = (ae && ae.dataset && ae.dataset.fkey && root.contains(ae))
    ? ae.dataset.fkey : null;
  let caret = null;
  if (key) { try { caret = ae.selectionStart; } catch (_) {} }
  render();
  if (!key) return;
  const back = root.querySelector(`[data-fkey="${cssEsc(key)}"]`);
  if (!back) return;
  back.focus();
  if (caret != null) { try { back.setSelectionRange(caret, caret); } catch (_) {} }
}

/* A match is identified server-side by a rank that moves. For anything that
   must survive a new scan, we key on its real position in the content
   stream. */
const sig = o => `${o.page}\u0001${o.unit}\u0001${o.start}\u0001${o.raw}`;
const fitOf = o => S.fits[sig(o)] || 'auto';
const ovOf  = o => S.overrides[sig(o)] || '';

/* ---------------------------------------------------------------- indicator */

let busyCount = 0;

function pushBusy(label) {
  busyCount++;
  $('#busyText').textContent = label;
  $('#busy').hidden = false;
}
function popBusy() {
  busyCount = Math.max(0, busyCount - 1);
  if (!busyCount) $('#busy').hidden = true;
}

/* ---------------------------------------------------------------- upload */

function initLanding() {
  const drop = $('#drop'), input = $('#file');
  drop.addEventListener('click', () => input.click());
  drop.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
  });
  input.addEventListener('change', () => {
    if (input.files.length) upload([...input.files]);
  });
  ['dragenter', 'dragover'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('hot'); }));
  ['dragleave', 'drop'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('hot'); }));
  drop.addEventListener('drop', e => {
    const fs = [...e.dataTransfer.files];
    if (fs.length) upload(fs);
  });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());
}

function resetSession() {
  S.docs = []; S.did = null;
  S.finders = [newRule()];
  S.dates = false;
  S.dateMode = 'map';
  S.mapTargets = {};
  S.shift = { days: 0, months: 0, years: 1 };
  S.anchors = {};
  S.page = 0; S.view = 'before'; S.selected = null; S.wipe = 55;

  $('#optDates').checked = false;
  $('#datesBody').hidden = true;
  $('#shDays').value = 0; $('#shMonths').value = 0; $('#shYears').value = 1;
  $$('#modeSeg button').forEach(b => b.classList.toggle('on', b.dataset.mode === 'map'));
  $$('.mode-body').forEach(x => { x.hidden = x.dataset.body !== 'map'; });
  $('#occFilter').value = '';
  $('#reportCard').hidden = true;
  $('#btnDownload').disabled = true;
}

const isPdf = f => /\.pdf$/i.test(f.name) || f.type === 'application/pdf';

async function upload(files) {
  const pdfs = files.filter(isPdf);
  if (!pdfs.length) return toast('No PDF in what you dropped.', true);
  if (pdfs.length < files.length)
    toast(`${files.length - pdfs.length} file(s) ignored: not PDFs.`);

  $('#landingStatus').hidden = false;
  $('#landingStatusText').textContent = pdfs.length > 1
    ? `Analyzing ${pdfs.length} documents…`
    : `Analyzing “${pdfs[0].name}”…`;

  const fd = new FormData();
  pdfs.forEach(f => fd.append('files', f));
  fd.append('locale', 'fr');   // date convention fallback, see buildBody()

  try {
    const data = await api('/api/lots', { method: 'POST', body: fd });
    resetSession();
    S.sid = data.id;
    S.docs = data.docs.map(newDoc);
    S.did = (S.docs.find(d => !d.error) || S.docs[0]).id;

    $('#landing').hidden = true;
    $('#workspace').hidden = false;
    renderMemory();
    renderAll();
    setTimeout(() => { const q = $('#ruleList .rule-in'); if (q) q.focus(); }, 30);
  } catch (e) {
    toast(e.message, true);
  } finally {
    $('#landingStatus').hidden = true;
  }
}

/* Adding documents to an already open batch: they inherit the current rules,
   so the search is re-run right after. */
async function addFiles(files) {
  const pdfs = [...files].filter(isPdf);
  if (!pdfs.length || !S.sid) return;
  const fd = new FormData();
  pdfs.forEach(f => fd.append('files', f));
  fd.append('locale', 'fr');   // date convention fallback, see buildBody()
  try {
    pushBusy('analyzing…');
    const data = await api(`/api/lots/${S.sid}/documents`, { method: 'POST', body: fd });
    const known = new Set(S.docs.map(d => d.id));
    for (const x of data.docs) if (!known.has(x.id)) S.docs.push(newDoc(x));
    renderRail(); setDocMeta();
    toast(`${pdfs.length} document${plural(pdfs.length)} added.`);
    runScan();
  } catch (e) {
    toast(e.message, true);
  } finally { popBusy(); }
}

async function removeDoc(did) {
  if (!S.sid || S.docs.length <= 1) {
    if (S.docs.length <= 1) toast('At least one document is required.');
    return;
  }
  try {
    await api(`/api/lots/${S.sid}/documents/${did}`, { method: 'DELETE' });
    S.docs = S.docs.filter(d => d.id !== did);
    if (S.did === did) {
      S.did = (S.docs.find(d => !d.error) || S.docs[0]).id;
      S.page = 0; S.selected = null;
      renderAll();
    } else {
      renderRail(); setDocMeta(); renderSummary();
    }
  } catch (e) { toast(e.message, true); }
}

function selectDoc(did) {
  if (did === S.did) return;
  const d = S.docs.find(x => x.id === did);
  if (!d || d.error) return;
  S.did = did;
  S.page = 0;
  S.selected = null;
  // The "after" views only make sense if THIS document was changed.
  if (S.view !== 'before' && !d.has_output) S.view = 'before';
  renderAll();
}

/* ---------------------------------------------------------------- render */

/* The rail tells, at a glance: which document I am looking at, which ones hold
   matches, which ones were changed. */
function renderRail() {
  const el = $('#railList');
  $('#railCount').textContent = String(S.docs.length);
  el.innerHTML = S.docs.map(d => {
    const on = d.id === S.did;
    const n = d.occ.length;
    let flag = '', sub;
    if (d.error) {
      flag = ' bad'; sub = 'unreadable';
    } else if (d.outcome) {
      const v = d.outcome.verification;
      const sev = v && v.severity;
      const bad = (d.outcome.notdef || []).length || sev === 'drift';
      flag = bad ? ' bad' : (d.outcome.refused ? ' warn' : ' ok');
      sub = d.outcome.applied
        ? `${d.outcome.applied} replaced`
        : 'nothing to replace';
    } else {
      flag = n ? ' ok' : '';
      sub = n ? `${n} found` : 'no match';
    }
    return `<button class="doc${on ? ' on' : ''}${d.error ? ' err' : ''}"
                    data-did="${esc(d.id)}" title="${esc(d.filename)}">
      <span class="doc-title">${esc(d.filename)}</span>
      <span class="doc-sub">
        <i class="doc-flag${flag}"></i>
        <span class="doc-hits${n ? ' some' : ' none'}">${esc(sub)}</span>
      </span>
      <span class="doc-x" data-del="${esc(d.id)}" title="remove from batch">✕</span>
    </button>`;
  }).join('');
}

function renderAll() {
  renderRail();
  renderFontCard();
  const d = D();
  $('#btnDownloadOne').disabled = !(d && d.has_output);
  $('#btnDownloadOne').hidden = S.docs.length < 2;
  renderWarnings();
  renderConvention();
  renderRuleList();
  renderMapList();
  renderOccList();
  renderPage();
  renderSummary();
  updateViewTabs();
  setDocMeta();
}

function setDocMeta() {
  const d = D();
  $('#docName').textContent = d ? d.filename : '—';
  if (!d) { $('#docMeta').textContent = ''; return; }
  const bits = [`${d.pages} p`, fmtBytes(d.size),
                `${d.occ.length} match${plural(d.occ.length, 'es')}`];
  if (S.docs.length > 1) {
    const t = lotTotals();
    bits.push(`batch: ${t.occ} in ${t.hit}/${t.docs} doc.`);
  }
  $('#docMeta').textContent = bits.join(' · ');
}

function renderWarnings() {
  const w = (S.analysis && S.analysis.warnings) || [];
  $('#warnCard').hidden = !w.length;
  $('#warnList').innerHTML = w.map(x => `<li>${esc(x)}</li>`).join('');
}

function renderConvention() {
  const conv = S.analysis && S.analysis.convention;
  const pill = $('#convPill');
  if (conv && conv !== 'unknown') {
    pill.hidden = false;
    pill.textContent = conv === 'DMY' ? 'day/month convention' : 'month/day convention';
  } else pill.hidden = true;
}

/* ----- rules ----- */

function renderRuleList() {
  const el = $('#ruleList');
  keepFocus(el, () => {
    el.innerHTML = S.finders.map(f => {
      const rid = esc(f.id);
      const rx = f.kind === 'regex';
      return `<div class="rule" data-id="${rid}">
        <div class="rule-fields">
          <input class="rule-in" type="text" spellcheck="false"
                 data-fkey="q:${rid}" data-rid="${rid}" data-f="query"
                 placeholder="text to find" value="${esc(f.query)}">
          <div class="rule-to"><span>→</span>
            <input class="rule-in" type="text" spellcheck="false"
                   data-fkey="r:${rid}" data-rid="${rid}" data-f="replace"
                   placeholder="replace with" value="${esc(f.replace)}">
          </div>
        </div>
        <div class="rule-foot">
          <div class="rule-toggles">
            <button class="tg mono-t${f.case_sensitive ? ' on' : ''}" data-rid="${rid}"
                    data-t="case" title="case sensitive">Aa</button>
            <button class="tg${f.whole_word ? ' on' : ''}" data-rid="${rid}"
                    data-t="word" title="whole word only">whole word</button>
            <button class="tg mono-t${rx ? ' on' : ''}" data-rid="${rid}"
                    data-t="regex" title="regular expression">.*</button>
          </div>
          <span class="rule-count mono">—</span>
          <button class="rule-del" data-rid="${rid}" title="delete this rule">✕</button>
        </div>
        <div class="rule-err" hidden></div>
        <div class="rule-help"${rx ? '' : ' hidden'}>Capture groups:
          <code>$1</code>, <code>$2</code>… reusable in the replacement.</div>
      </div>`;
    }).join('');
  });
  // Adding or deleting a row does not invalidate the other counters.
  updateRuleMeta();
}

/* The scan returns counters and possible syntax errors. We drop them into the
   existing DOM: rebuilding the list would steal the caret mid-keystroke. */
function updateRuleMeta() {
  // Rules apply to the whole batch: so must the counter.
  const t = lotTotals();
  const counts = t.byFinder, errors = t.errors;
  for (const f of S.finders) {
    const node = $(`#ruleList .rule[data-id="${cssEsc(f.id)}"]`);
    if (!node) continue;
    const has = f.query.trim() !== '';
    const n = counts[f.id] || 0;
    const cEl = node.querySelector('.rule-count');
    cEl.textContent = has ? String(n) : '—';
    cEl.classList.toggle('on', has && n > 0);

    const err = errors[f.id] || '';
    const eEl = node.querySelector('.rule-err');
    eEl.textContent = err;
    eEl.hidden = !err;
    node.classList.toggle('err', !!err);
  }
}

/* ----- list of distinct dates (mapping mode) ----- */

function distinctDates() {
  const m = new Map();
  for (const o of S.occ) {
    if (o.kind !== 'dates' || !o.key) continue;
    if (!m.has(o.key)) m.set(o.key, { key: o.key, count: 0, sample: o.raw, partial: o.partial });
    m.get(o.key).count++;
  }
  return [...m.values()].sort((a, b) => a.key.localeCompare(b.key));
}

function renderMapList() {
  const el = $('#mapList');
  const list = distinctDates();
  keepFocus(el, () => {
    if (!list.length) {
      el.innerHTML = '<div class="empty">No date detected yet.</div>';
      return;
    }
    el.innerHTML = list.map(d => {
      const val = S.mapTargets[d.key] || '';
      const type = d.key.length === 10 ? 'date' : 'text';
      const ph = d.key.length === 10 ? '' : d.key.length === 7 ? 'YYYY-MM' : 'YYYY';
      return `<div class="map-row${val ? ' done' : ''}" data-key="${esc(d.key)}">
        <span class="map-src">${esc(prettyKey(d.key))}<small>×${d.count}</small></span>
        <span class="map-arrow">→</span>
        <input type="${type}" value="${esc(val)}" placeholder="${ph}"
               data-fkey="m:${esc(d.key)}" data-mapkey="${esc(d.key)}">
      </div>`;
    }).join('');
  });
}

/* ----- matches ----- */

function strategyBadge(sim) {
  if (sim.strategy === 'inplace')
    return '<span class="badge b-aligne">pixel-identical</span>';
  if (sim.strategy === 'words')
    return '<span class="badge b-regroupe">word by word</span>';
  if (sim.strategy === 'recomposed')
    return '<span class="badge b-regroupe">recomposed</span>';
  // A block that font repair will lift is not a block: showing it in red would
  // talk the user out of a replacement that will work perfectly well.
  if (sim.repairable && $('#optRepair').checked)
    return '<span class="badge b-warn">after font repair</span>';
  return '<span class="badge b-refus">blocked</span>';
}

/* How the width was handled: the first thing the user looks at when in doubt
   about a replacement. */
function fitBadge(sim) {
  if (!sim.ok) return '';
  if (sim.fit_used === 'fit') {
    const pct = sim.scaled ? Math.round((sim.scaled - 1) * 100) : 0;
    if (!pct) return '<span class="badge b-info">fitted</span>';
    return `<span class="badge b-info">fitted ${pct > 0 ? '+' : '−'}${Math.abs(pct)} %</span>`;
  }
  if (sim.fit_used === 'push') {
    const d = sim.width_delta || 0;
    if (Math.abs(d) < 0.5) return '<span class="badge b-info">nothing moved</span>';
    return `<span class="badge b-info">line ${d > 0 ? 'pushed +' : 'pulled back −'}${Math.abs(d).toFixed(0)} pt</span>`;
  }
  return '<span class="badge b-info">exact</span>';
}

function fitPicker(o, sim) {
  const cur = fitOf(o);
  const btns = ['auto', 'exact', 'fit', 'push'].map(k =>
    `<button data-fit="${k}" data-oid="${esc(o.id)}"${cur === k ? ' class="on"' : ''}>${FIT_LABELS[k]}</button>`
  ).join('');
  return `<div class="occ-fit"><span class="occ-fit-k">width</span>${btns}</div>`;
}

function renderOccList() {
  const el = $('#occList');
  const q = ($('#occFilter').value || '').toLowerCase();
  const multi = S.finders.filter(f => f.query.trim()).length + (S.dates ? 1 : 0) > 1;

  const items = S.occ.filter(o => !q
    || o.raw.toLowerCase().includes(q)
    || (o.target || '').toLowerCase().includes(q)
    || ((o.context_before || '') + (o.context_after || '')).toLowerCase().includes(q));

  $('#occCount').textContent = `${S.checked.size}/${S.occ.length}`;

  keepFocus(el, () => {
    if (!S.occ.length) {
      el.innerHTML = '<div class="empty">Write a rule to search for text.</div>';
      return;
    }
    if (!items.length) {
      el.innerHTML = '<div class="empty">No match fits the filter.</div>';
      return;
    }

    el.innerHTML = items.map(o => {
      const sim = S.sim[o.id];
      const on = S.checked.has(o.id);
      const selected = S.selected === o.id;
      const badges = [];

      if (sim) {
        badges.push(strategyBadge(sim));
        const fb = fitBadge(sim);
        if (fb) badges.push(fb);
        if (sim.overlap_risk)
          badges.push('<span class="badge b-warn">overlap risk</span>');
        if (!sim.ok && sim.repairable)
          badges.push(`<span class="badge b-info">glyphs taken from ${esc(sim.repairable)}</span>`);
        if (!sim.ok && !sim.repairable && sim.missing && sim.missing.length)
          badges.push(`<span class="badge b-warn">missing glyphs: ${esc(sim.missing.join(' '))}</span>`);
      }
      if (multi) badges.push(`<span class="badge b-info mono">${esc(o.finder)}</span>`);
      if (o.ambiguous) badges.push('<span class="badge b-warn">ambiguous format</span>');
      if (o.invisible) badges.push('<span class="badge b-warn">invisible layer</span>');
      if (o.partial) badges.push('<span class="badge b-info">partial date</span>');
      if (o.fonts && o.fonts.length) badges.push(`<span class="badge b-info">${esc(o.fonts[0])}</span>`);

      const shown = sim ? sim.new : (ovOf(o) || o.target || '');
      const newTxt = shown
        ? `<span class="occ-arrow">→</span><span class="occ-new${sim && !sim.ok && !(sim.repairable && $('#optRepair').checked) ? ' bad' : ''}">${esc(shown)}</span>`
        : '';

      const why = (selected && sim && sim.fit_reason)
        ? `<div class="occ-why">${esc(sim.fit_reason)}</div>` : '';

      const picker = (selected && sim &&
        (Math.abs(sim.width_delta || 0) > 0.01 || fitOf(o) !== 'auto'))
        ? fitPicker(o, sim) : '';

      const free = `<div class="occ-manual"><input type="text" spellcheck="false"
            data-fkey="o:${esc(o.id)}" data-ov="${esc(o.id)}"
            value="${esc(ovOf(o))}" placeholder="custom text (optional)"></div>`;

      return `<div class="occ${selected ? ' sel' : ''}${on ? '' : ' unchecked'}"
                   data-id="${esc(o.id)}"${sim && sim.fit_reason ? ` title="${esc(sim.fit_reason)}"` : ''}>
        <div class="occ-top">
          <input type="checkbox" data-check="${esc(o.id)}" ${on ? 'checked' : ''}>
          <span class="occ-raw">${esc(o.raw)}</span>
          ${newTxt}
          <span class="occ-page">p.${o.page + 1}</span>
        </div>
        <div class="occ-ctx">…${esc(o.context_before)}<b>${esc(o.raw)}</b>${esc(o.context_after)}…</div>
        <div class="occ-badges">${badges.join('')}</div>
        ${why}
        ${picker}
        ${free}
        ${sim && !sim.ok && sim.reason && !(sim.repairable && $('#optRepair').checked)
          ? `<div class="occ-ctx occ-why-full" style="color:var(--red)">${esc(sim.reason)}</div>` : ''}
      </div>`;
    }).join('');
  });
}

function selectOcc(id) {
  S.selected = id;
  const o = S.occ.find(x => x.id === id);
  $$('#occList .occ').forEach(n => n.classList.toggle('sel', n.dataset.id === id));
  const node = $(`#occList .occ[data-id="${cssEsc(id)}"]`);
  if (node) node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  if (!o) return;
  loadPage(o.page);
  drawOverlay();
  const hl = $(`#pg${o.page} .hl[data-id="${cssEsc(id)}"]`);
  if (hl) hl.scrollIntoView({ block: 'center', behavior: 'smooth' });
  else goToPage(o.page);
  syncPageIndicator(true);
}

/* ----- page ----- */

/* The sheet used to be rendered at a fixed 130 dpi, then displayed at whatever
   size its own resolution gave it. On a dense screen — two physical pixels per
   CSS pixel — one was therefore looking at an image twice too coarse, hence the
   feeling of compression even though the PNG is lossless.

   The display width is now fixed, and we ask the server for exactly the number
   of pixels the screen is really going to paint. */
function sheetLayout() {
  const size = (S.analysis && S.analysis.page_sizes[S.page]) || [595, 842];
  const scroll = $('#stageScroll');
  const avail = Math.max(320, (scroll ? scroll.clientWidth : 900) - 52);
  const natural = size[0] * 96 / 72;              // 1 pt = 1.333 CSS px
  const cssWidth = Math.round(Math.min(avail, natural * 2));
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const dpi = Math.round(Math.max(120, Math.min(300, 72 * cssWidth * dpr / size[0])));
  return { cssWidth, dpi };
}

/* ==========================================================================
   Viewer: every page, one after the other.

   Three constraints dictate the shape:
   - a 300-page document cannot load 300 images; we only render what comes
     close to the screen, and unload what moves away from it;
   - the scrollbar must be correct from the first frame, otherwise the page
     jumps under the fingers — hence containers sized ahead of time from
     `page_sizes`;
   - the comparator has a single handle, centred on what is on screen: hunting
     for it page by page would be absurd.
   ========================================================================== */

const PRELOAD = 2;     // pages loaded on either side of the visible area
const KEEP = 6;        // beyond that, the image is released

let pageObserver = null;

function pageEl(i) { return document.getElementById('pg' + i); }

function buildPages() {
  const wrap = $('#pages');
  const a = S.analysis;
  if (!a) { wrap.innerHTML = ''; return; }

  const { cssWidth, dpi } = sheetLayout();
  S.dpi = dpi;

  wrap.innerHTML = '';
  for (let i = 0; i < a.pages; i++) {
    const size = a.page_sizes[i] || a.page_sizes[0] || [595, 842];
    const h = Math.round(cssWidth * size[1] / size[0]);
    const el = document.createElement('div');
    el.className = 'page pending';
    el.id = 'pg' + i;
    el.dataset.p = String(i);
    el.style.width = cssWidth + 'px';
    el.style.height = h + 'px';
    el.innerHTML = `<span class="page-no">${i + 1}</span>`;
    wrap.appendChild(el);
  }

  if (pageObserver) pageObserver.disconnect();
  pageObserver = new IntersectionObserver(onPagesVisible, {
    root: $('#stageScroll'),
    rootMargin: `${PRELOAD * 100}% 0px`,
    threshold: 0,
  });
  $$('#pages .page').forEach(el => pageObserver.observe(el));
  syncPageIndicator();
}

function onPagesVisible(entries) {
  for (const e of entries) {
    const i = +e.target.dataset.p;
    if (e.isIntersecting) loadPage(i);
  }
  pruneFarPages();
  syncPageIndicator();
}

function pageSources(i) {
  const base = `/api/lots/${S.sid}/documents/${S.did}`;
  const dpi = S.dpi || 130;
  const d = D();
  const touched = (d && d.outcome && d.outcome.touched_pages) || [];
  if (S.view === 'diff') {
    // A page the apply did not touch has nothing to show in diff view: we
    // display the normal render and say so, rather than two renders for nothing.
    return touched.includes(i)
      ? { main: `${base}/diff/${i}.png?dpi=${dpi}`, note: null }
      : { main: `${base}/page/${i}.png?v=after&dpi=${dpi}`, note: 'page unchanged' };
  }
  if (S.view === 'compare') {
    return {
      main: `${base}/page/${i}.png?v=before&dpi=${dpi}`,
      top: `${base}/page/${i}.png?v=after&dpi=${dpi}`,
    };
  }
  return { main: `${base}/page/${i}.png?v=${S.view}&dpi=${dpi}` };
}

function loadPage(i) {
  const el = pageEl(i);
  if (!el || el.dataset.loaded === S.view + ':' + S.dpi) return;
  el.dataset.loaded = S.view + ':' + S.dpi;

  const src = pageSources(i);
  el.querySelectorAll('.page-img, .page-cut, .page-note, .overlay').forEach(n => n.remove());

  const main = document.createElement('img');
  main.className = 'page-img';
  main.alt = `page ${i + 1}`;
  main.src = src.main;
  main.onload = () => { el.classList.remove('pending'); drawOverlay(i); };
  el.insertBefore(main, el.firstChild);

  if (src.top) {
    const top = document.createElement('img');
    top.className = 'page-img top';
    top.alt = `page ${i + 1} modified`;
    top.src = src.top;
    el.appendChild(top);
    const cut = document.createElement('div');
    cut.className = 'page-cut';
    el.appendChild(cut);
  }
  if (src.note) {
    const n = document.createElement('span');
    n.className = 'page-note';
    n.textContent = src.note;
    el.appendChild(n);
  }

  const ov = document.createElement('div');
  ov.className = 'overlay';
  el.appendChild(ov);

  applyWipe();
  drawOverlay(i);
}

function unloadPage(i) {
  const el = pageEl(i);
  if (!el || !el.dataset.loaded) return;
  delete el.dataset.loaded;
  el.classList.add('pending');
  el.querySelectorAll('.page-img, .page-cut, .page-note, .overlay').forEach(n => n.remove());
}

function pruneFarPages() {
  const cur = S.page;
  $$('#pages .page').forEach(el => {
    const i = +el.dataset.p;
    if (el.dataset.loaded && Math.abs(i - cur) > KEEP) unloadPage(i);
  });
}

/* The "current" page is the one that fills the visible area the most. */
function visiblePage() {
  const scroll = $('#stageScroll');
  const r = scroll.getBoundingClientRect();
  let best = S.page, bestArea = -1;
  for (const el of $$('#pages .page')) {
    const b = el.getBoundingClientRect();
    const overlap = Math.min(b.bottom, r.bottom) - Math.max(b.top, r.top);
    if (overlap > bestArea) { bestArea = overlap; best = +el.dataset.p; }
  }
  return best;
}

let hudTimer = null;

function syncPageIndicator(flash) {
  if (!S.analysis) return;
  const n = S.analysis.pages;
  const p = visiblePage();
  const moved = p !== S.page;
  S.page = p;
  $('#pgLabel').textContent = `${p + 1}/${n}`;
  $('#pgPrev').disabled = p === 0;
  $('#pgNext').disabled = p >= n - 1;

  // A discreet reminder in the middle of the screen while scrolling: on a long
  // document, the counter in the top bar is too far from the eye.
  if (n > 1 && (flash || moved)) {
    const hud = $('#pageHud');
    hud.textContent = `page ${p + 1} / ${n}`;
    hud.hidden = false;
    hud.classList.add('show');
    clearTimeout(hudTimer);
    hudTimer = setTimeout(() => hud.classList.remove('show'), 900);
  }
}

function goToPage(i, smooth = true) {
  if (!S.analysis) return;
  const n = S.analysis.pages;
  i = Math.max(0, Math.min(n - 1, i));
  const el = pageEl(i);
  if (!el) return;
  loadPage(i);
  const scroll = $('#stageScroll');
  if (!smooth) scroll.classList.add('no-smooth');
  scroll.scrollTop = el.offsetTop - 26;
  if (!smooth) requestAnimationFrame(() => scroll.classList.remove('no-smooth'));
  syncPageIndicator(true);
}

/* Rebuilds the viewer: document, view or resolution change. */
function renderPage() {
  if (!S.analysis) { $('#pages').innerHTML = ''; return; }
  const keep = S.page;
  buildPages();
  $('#wipeKnob').hidden = S.view !== 'compare';
  goToPage(keep, false);
  applyWipe();
  renderLegend();
}

function applyWipe() {
  for (const el of $$('#pages .page')) {
    const top = el.querySelector('.page-img.top');
    if (top) top.style.clipPath = `inset(0 0 0 ${S.wipe}%)`;
    const cut = el.querySelector('.page-cut');
    if (cut) cut.style.left = `calc(${S.wipe}% - 1px)`;
  }
  placeKnob();
}

/* The handle floats at the vertical centre of what is on screen, aligned on the
   page column: it stays under the hand however far one scrolls. */
function placeKnob() {
  const knob = $('#wipeKnob');
  if (S.view !== 'compare') { knob.hidden = true; return; }
  const el = pageEl(S.page) || $('#pages .page');
  const stage = $('#stage');
  if (!el || !stage) { knob.hidden = true; return; }
  const a = el.getBoundingClientRect(), b = stage.getBoundingClientRect();
  knob.hidden = false;
  knob.style.left = (a.left - b.left + a.width * S.wipe / 100) + 'px';
}

/* A cut in the middle of a margin shows nothing: we place it on the first
   change. */
function centerWipeOnChange() {
  if (!S.analysis) return;
  const changed = S.occ.filter(o => o.bbox && S.sim[o.id] && S.sim[o.id].ok);
  if (!changed.length) return;
  const o = changed.find(x => x.page === S.page) || changed[0];
  const size = S.analysis.page_sizes[o.page];
  if (!size) return;
  S.page = o.page;
  S.wipe = Math.max(6, Math.min(94, (o.bbox[0] + o.bbox[2]) / 2 / size[0] * 100));
}

function renderLegend() {
  const el = $('#stageLegend');
  if (S.view === 'diff') {
    el.innerHTML = `
      <span class="legend-dot"><i style="background:#10a058"></i> expected change</span>
      <span class="legend-dot"><i style="background:#dc2626"></i> drift outside the zone — or a line moved on purpose</span>
      <span class="legend-dot" style="margin-left:auto">check rendered at 130 dpi</span>`;
  } else if (S.view === 'compare') {
    el.innerHTML = `<span class="legend-dot"><i style="background:#5d6472"></i> original on the left</span>
      <span class="legend-dot"><i style="background:#c9f24d"></i> result on the right</span>
      <span class="legend-dot" style="margin-left:auto">drag the handle, or click anywhere on the page</span>`;
  } else {
    const nb = S.occ.filter(o => o.page === S.page).length;
    el.innerHTML = `<span class="legend-dot"><i style="background:#c9f24d"></i> ${nb} match${plural(nb, 'es')} on this page — click to target one</span>`;
  }
}

function drawOverlay(only) {
  if (!S.analysis) return;
  // The rectangles come from the analysis of the original: putting them on a
  // modified render would point at possibly stale positions.
  const show = S.view === 'before';

  const pages = only == null ? $$('#pages .page') : [pageEl(only)].filter(Boolean);
  for (const el of pages) {
    const ov = el.querySelector('.overlay');
    if (!ov) continue;
    ov.innerHTML = '';
    if (!show) continue;

    const i = +el.dataset.p;
    const size = S.analysis.page_sizes[i];
    const w = el.clientWidth, h = el.clientHeight;
    if (!w || !size) continue;
    const sx = w / size[0], sy = h / size[1];

    for (const o of S.occ) {
      if (o.page !== i || !o.bbox) continue;
      const sim = S.sim[o.id];
      const d = document.createElement('div');
      d.className = 'hl'
        + (S.checked.has(o.id) ? '' : ' off')
        + (sim && !sim.ok && !(sim.repairable && $('#optRepair').checked) ? ' bad' : '')
        + (S.selected === o.id ? ' sel' : '');
      d.dataset.id = o.id;
      d.style.left   = (o.bbox[0] * sx - 2) + 'px';
      d.style.top    = (o.bbox[1] * sy - 2) + 'px';
      d.style.width  = ((o.bbox[2] - o.bbox[0]) * sx + 4) + 'px';
      d.style.height = ((o.bbox[3] - o.bbox[1]) * sy + 4) + 'px';
      d.title = o.raw + (sim ? ` → ${sim.new}` : (o.target ? ` → ${o.target}` : ''));
      if (sim && S.selected === o.id) {
        const tag = document.createElement('div');
        tag.className = 'hl-tag';
        const repairing = sim.repairable && $('#optRepair').checked;
        tag.textContent = (sim.ok || repairing)
          ? `→ ${sim.new}` : `✕ ${sim.reason || 'blocked'}`;
        d.appendChild(tag);
      }
      d.addEventListener('click', () => selectOcc(o.id));
      ov.appendChild(d);
    }
  }
}

/* ---------------------------------------------------------------- requests */

function activeFinders() {
  const out = S.finders
    .filter(f => f.query.trim() !== '')
    .map(f => ({
      id: f.id, kind: f.kind, query: f.query, replace: f.replace,
      case_sensitive: f.case_sensitive, whole_word: f.whole_word,
      loose: true, enabled: true,
    }));
  if (S.dates) out.push({
    id: 'dates', kind: 'dates', query: '', replace: '',
    case_sensitive: false, whole_word: false, loose: true, enabled: true,
  });
  return out;
}

function buildDateRule() {
  if (!S.dates || S.dateMode === 'manual') return { type: 'manual', pairs: {} };
  if (S.dateMode === 'map') return { type: 'map', pairs: S.mapTargets };
  return { type: 'shift', days: S.shift.days, months: S.shift.months,
           years: S.shift.years, pairs: {} };
}

/* Fine settings are stored by match SIGNATURE (stable); the server, for its
   part, thinks in match identifiers, which shift as soon as a rule changes. The
   translation happens here, document by document, with the freshest list we
   have.

   Exclusion, on the other hand, goes out as is: it is a blacklist of stable
   identities. A whitelist computed by the client would become wrong as soon as
   a rule changed, and would silently drop work. */
function perDocTweaks() {
  const out = {};
  for (const d of lotDocs()) {
    const overrides = {}, fits = {};
    for (const o of d.occ) {
      const k = sig(o);
      if (d.overrides[k]) overrides[o.id] = d.overrides[k];
      const f = d.fits[k];
      if (f && f !== 'auto') fits[o.id] = f;
    }
    out[d.id] = { overrides, fits, anchors: {}, exclude: [...d.unchecked] };
  }
  return out;
}

function buildBody() {
  return {
    finders: activeFinders(),
    date_rule: buildDateRule(),
    per_doc: perDocTweaks(),
    // Fallback day/month convention for dates the document itself does not
    // disambiguate — not the interface language. The engine prefers the
    // evidence it finds in the document.
    locale: 'fr',
    auto_repair: $('#optRepair').checked,
    verify: true,
  };
}

/* Check by default everything visible, except what the user explicitly
   unchecked — the signature survives an identifier reshuffle. */
function rebuildChecked(d) {
  d = d || D();
  if (!d) return;
  d.checked = new Set();
  for (const o of d.occ) {
    if (o.invisible) continue;
    if (d.unchecked.has(sig(o))) continue;
    d.checked.add(o.id);
  }
}

/* Nothing to simulate as long as no checked match carries a target. */
function hasSimWork() {
  for (const d of lotDocs()) {
    for (const o of d.occ) {
      if (!d.checked.has(o.id)) continue;
      const t = (d.overrides[sig(o)] || '') || o.target || '';
      if (t && t !== o.raw) return true;
    }
  }
  return false;
}

let scanToken = 0, simToken = 0;

const scheduleScan = debounce(runScan, 500);
const scheduleSim  = debounce(runSim, 400);

/* The rules changed: the list of matches itself is stale. */
async function runScan() {
  if (!S.sid) return;
  const finders = activeFinders();

  if (!finders.length) {
    scanToken++; simToken++;
    for (const d of lotDocs()) {
      d.occ = []; d.sim = {}; d.checked = new Set();
      if (d.analysis) { d.analysis.finder_counts = {}; d.analysis.finder_errors = {}; }
    }
    S.selected = null;
    updateRuleMeta();
    renderRail();
    renderMapList(); renderOccList(); drawOverlay(); renderSummary();
    setDocMeta(); renderLegend();
    return;
  }

  const mine = ++scanToken;
  pushBusy(S.docs.length > 1 ? `searching ${S.docs.length} documents…` : 'searching…');
  try {
    const data = await jsonPost(`/api/lots/${S.sid}/scan`, buildBody());
    if (mine !== scanToken) return;

    for (const r of data.docs) {
      const d = S.docs.find(x => x.id === r.id);
      if (!d) continue;
      if (r.error) { d.error = r.error; continue; }
      d.analysis = r.analysis;
      d.occ = r.analysis.occurrences || [];
      d.sim = {};
      rebuildChecked(d);
    }
    if (S.selected && !S.occ.some(o => o.id === S.selected)) S.selected = null;

    updateRuleMeta();
    renderRail();
    renderWarnings();
    renderConvention();
    renderMapList();
    renderOccList();
    drawOverlay();
    renderLegend();
    setDocMeta();
    renderSummary();
    runSim();
  } catch (e) {
    if (mine === scanToken) toast('Search: ' + e.message, true);
  } finally {
    popBusy();
  }
}

/* Only a fine setting or the selection changed: the list holds, we only
   recompute the result. */
async function runSim() {
  if (!S.sid) return;
  if (!activeFinders().length || !hasSimWork()) {
    simToken++;
    for (const d of lotDocs()) d.sim = {};
    renderOccList(); drawOverlay(); renderSummary();
    return;
  }

  const mine = ++simToken;
  pushBusy('simulating…');
  try {
    const out = await jsonPost(`/api/lots/${S.sid}/simulate`, buildBody());
    if (mine !== simToken) return;
    for (const r of out.docs) {
      const d = S.docs.find(x => x.id === r.id);
      if (!d || !r.outcome) continue;
      d.sim = {};
      for (const x of r.outcome.results) d.sim[x.id] = x;
    }
    renderOccList();
    renderFontCard();
    drawOverlay();
    renderSummary();
  } catch (e) {
    if (mine === simToken) toast('Simulation: ' + e.message, true);
  } finally {
    popBusy();
  }
}

/* ---------------------------------------------------------------- summary */

function tallySim() {
  const c = { inplace: 0, words: 0, recomposed: 0, blocked: 0, repairable: 0 };
  const fits = { exact: 0, fit: 0, push: 0 };
  const repair = $('#optRepair').checked;
  for (const id in S.sim) {
    const r = S.sim[id];
    if (!r.ok && r.repairable && repair) c.repairable++;
    else c[r.strategy] = (c[r.strategy] || 0) + 1;
    if (r.ok && r.fit_used) fits[r.fit_used] = (fits[r.fit_used] || 0) + 1;
  }
  return { c, fits };
}

function renderSummary() {
  if (!S.analysis) return;
  const el = $('#actionSummary');
  const n = Object.keys(S.sim).length;

  if (!n) {
    el.innerHTML = `<span style="color:var(--dimmer)">Write a rule to see the simulation.</span>`;
    $('#btnApply').disabled = true;
    return;
  }

  const { c, fits } = tallySim();
  const recomposed = c.recomposed + c.words;
  const moved = fits.push + fits.fit;

  el.innerHTML = `
    <span><b class="mono">${n}</b> replacement${plural(n)}</span>
    ${c.inplace ? `<span class="badge b-aligne">${c.inplace} pixel-identical</span>` : ''}
    ${recomposed ? `<span class="badge b-regroupe">${recomposed} recomposed</span>` : ''}
    ${moved ? `<span class="badge b-info">${moved} width-adjusted</span>` : ''}
    ${c.repairable ? `<span class="badge b-warn">${c.repairable} after font repair</span>` : ''}
    ${c.blocked ? `<span class="badge b-refus">${c.blocked} blocked</span>` : ''}`;

  $('#btnApply').disabled = (c.inplace + c.words + c.recomposed + c.repairable) === 0;
}

/* ---------------------------------------------------------------- apply */

async function applyEdits() {
  if (S.busy) return;
  // A search in flight means the rules changed and the list is not up to date
  // yet: we let it finish before writing anything at all.
  if (busyCount) { toast('Search in progress, try again in a moment.'); return; }
  S.busy = true;
  const btn = $('#btnApply');
  const old = btn.textContent;
  btn.textContent = 'Applying…'; btn.disabled = true;
  pushBusy('applying…');

  try {
    const data = await jsonPost(`/api/lots/${S.sid}/apply`, buildBody());
    memRemember(S.finders);

    for (const r of data.docs) {
      const d = S.docs.find(x => x.id === r.id);
      if (!d || !r.outcome) continue;
      d.outcome = r.outcome;
      d.has_output = r.outcome.applied > 0;
      d.sim = {};
      for (const x of r.outcome.results) d.sim[x.id] = x;
    }

    const agg = aggregate();
    renderReport(agg);
    renderRail();
    renderOccList();
    // The "source font" card is built on the simulation, which runs without
    // repair: after a successful apply it has no reason to be there.
    renderFontCard();
    renderSummary();

    // Move to a document that actually changed: staying in front of an
    // untouched document after an apply gives the impression that nothing
    // worked.
    const cur = D();
    if (!cur || !cur.has_output) {
      const first = S.docs.find(d => d.has_output);
      if (first) { S.did = first.id; S.page = 0; S.selected = null; }
    }
    const doc = D();
    const own = doc && doc.outcome;
    S.view = (own && own.verification && own.verification.severity === 'drift')
      ? 'diff' : (doc && doc.has_output ? 'compare' : 'before');
    if (own && own.touched_pages && own.touched_pages.length &&
        !own.touched_pages.includes(S.page)) S.page = own.touched_pages[0];

    renderWarnings(); renderMapList(); setDocMeta();
    centerWipeOnChange();
    updateViewTabs();
    renderPage();
    $('#btnDownload').disabled = !agg.modified;
    $('#btnDownload').textContent = agg.modified > 1
      ? `Download all (${agg.modified})` : 'Download';

    const many = S.docs.length > 1;
    const where = many ? ` across ${agg.modified} document${plural(agg.modified)}` : '';
    if (agg.notdef.length)
      toast(`${agg.notdef.length} character${plural(agg.notdef.length)} come out as empty boxes — see the report.`, true);
    else if (!agg.applied)
      toast('No match to replace.', true);
    else if (agg.severity === 'exact')
      toast(`${agg.applied} replacement${plural(agg.applied)}${where} — nothing else moved.`);
    else if (agg.severity === 'negligible')
      toast(`${agg.applied} replacement${plural(agg.applied)}${where} — residual difference below the perception threshold.`);
    else
      toast(`${agg.applied} replacement${plural(agg.applied)}${where}, but content moved. Check the Verify tab.`, true);
  } catch (e) {
    toast('Apply: ' + e.message, true);
  } finally {
    btn.textContent = old; btn.disabled = false; S.busy = false;
    popBusy();
  }
}

/* The report speaks for the whole batch. We aggregate, keeping the worst
   verdict: a broken document must not dilute into the average of the others. */
const SEV_RANK = { exact: 0, negligible: 1, drift: 2 };

function aggregate() {
  const a = {
    applied: 0, refused: 0, modified: 0, docs: 0, untouched: [],
    strategy_counts: {}, fit_counts: {}, repairs: [], notdef: [], refusedList: [],
    severity: 'exact', unexpected_total: 0, pages: 0, pages_drifting: 0,
    integrity: 1, perDoc: [],
  };
  for (const d of S.docs) {
    const o = d.outcome;
    if (!o) continue;
    a.docs++;
    a.applied += o.applied || 0;
    a.refused += o.refused || 0;
    if (d.has_output) a.modified++; else a.untouched.push(d.filename);
    for (const k in (o.strategy_counts || {}))
      a.strategy_counts[k] = (a.strategy_counts[k] || 0) + o.strategy_counts[k];
    for (const k in (o.fit_counts || {}))
      a.fit_counts[k] = (a.fit_counts[k] || 0) + o.fit_counts[k];
    for (const r of (o.repairs || [])) a.repairs.push({ ...r, doc: d.filename });
    for (const n of (o.notdef || [])) a.notdef.push({ ...n, doc: d.filename });
    const v = o.verification;
    if (v) {
      a.unexpected_total += v.unexpected_total || 0;
      a.pages += (v.pages || []).length;
      a.pages_drifting += v.pages_drifting || 0;
      a.integrity = Math.min(a.integrity, v.integrity != null ? v.integrity : 1);
      if (SEV_RANK[v.severity] > SEV_RANK[a.severity]) a.severity = v.severity;
    }
    for (const r of (o.results || [])) if (!r.ok) a.refusedList.push({ ...r, doc: d.filename });
    a.perDoc.push({ name: d.filename, applied: o.applied || 0,
                    refused: o.refused || 0,
                    severity: (v && v.severity) || 'exact',
                    notdef: (o.notdef || []).length });
  }
  // The report reuses the shape of a single result.
  a.verification = {
    severity: a.severity, unexpected_total: a.unexpected_total,
    pages_drifting: a.pages_drifting, integrity: a.integrity,
    pages: new Array(a.pages),
  };
  return a;
}

function fitSentence(fits) {
  const bits = [];
  if (fits.exact) bits.push(`<b>${fits.exact}</b> with nothing moved`);
  if (fits.fit)   bits.push(`<b>${fits.fit}</b> fitted in width`);
  if (fits.push)  bits.push(`<b>${fits.push}</b> line${plural(fits.push)} pushed`);
  return bits.join('<span class="sep">·</span>');
}

/* When an embedded font is incomplete or unreadable, the only way out is to
   supply the original font file. The API has accepted it from the start; it
   just had to be offered at the right moment. */
function renderFontCard() {
  const blocked = new Map();
  let injectable = false;
  const every = [];
  for (const d of lotDocs()) for (const id in d.sim) every.push(d.sim[id]);
  for (const r of every) {
    if (r.ok || !r.missing || !r.missing.length) continue;
    if (r.repairable) injectable = true;
    for (const f of (r.fonts || ['?'])) {
      const e = blocked.get(f) || { chars: new Set() };
      r.missing.forEach(c => e.chars.add(c));
      blocked.set(f, e);
    }
  }
  const card = $('#fontCard');
  if (!blocked.size) { card.hidden = true; return; }
  card.hidden = false;
  const bits = [...blocked.entries()].map(([f, e]) =>
    `<b>${esc(f)}</b> (${[...e.chars].join(' ')})`);

  // Glyph grafting only works on TrueType programs. On PostScript/CFF, sending
  // the font will change nothing: saying so plainly beats letting the user hunt
  // for a file for nothing.
  const canInject = injectable || [...blocked.keys()].some(
    f => every.some(r => r.fonts && r.fonts.includes(f) && r.repairable));
  $('#fontHint').innerHTML = canInject
    ? `${bits.join(', ')}: the embedded font does not provide these characters.
       Upload the original font file and the missing glyphs will be taken from it.`
    : `${bits.join(', ')}: the embedded font is PostScript/CFF, a format I cannot
       graft glyphs into yet. Uploading the source font will therefore not
       unblock these matches — a known limitation.`;
  // Do not offer an action that would serve no purpose.
  $('#fontDrop').hidden = !canInject;
}

async function sendFont(file) {
  if (!S.sid || !file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    pushBusy('analyzing the font…');
    const r = await api(`/api/lots/${S.sid}/fonts`, { method: 'POST', body: fd });
    const names = (r.names || []).join(' · ') || file.name;
    $('#fontList').insertAdjacentHTML('beforeend',
      `<div class="font-item">✓ ${esc(names)}</div>`);
    toast(`Font “${names}” saved.`);
    scheduleSim();
  } catch (e) {
    toast('Font: ' + e.message, true);
  } finally { popBusy(); }
}

function renderReport(out) {
  const v = out.verification;
  const c = out.strategy_counts || {};
  const fits = out.fit_counts || {};
  const recomposed = (c.recomposed || 0) + (c.words || 0);
  const tot = (c.inplace || 0) + recomposed + (c.blocked || 0) || 1;

  const score = v ? (v.integrity * 100).toFixed(4) : '—';
  const sev = v ? v.severity : 'exact';
  const pushed = fits.push || 0;

  /* Three verdicts, not two. A render is not perfectly deterministic: a few
     antialiasing pixels at the edge of a zone are not moved content, and
     confusing the two would be crying wolf. */
  const SEAL = {
    exact: ['', 'Integrity verified',
      'Not a single pixel changed outside the replaced zones.'],
    negligible: ['', 'Within tolerance',
      `${v ? v.unexpected_total : 0} pixels of residual difference, isolated and below
       the perception threshold — rasterization noise, not moved content.`],
    drift: [' bad', 'Content moved',
      `Content moved on ${v ? v.pages_drifting : '?'} page${plural(v ? v.pages_drifting : 0)}
       outside the replaced zones.`],
  };
  let [cls, label, note] = SEAL[sev] || SEAL.drift;
  if ((out.notdef || []).length) {
    cls = ' bad';
    label = 'Characters with no outline';
    note = `${out.notdef.length} character${plural(out.notdef.length)} come out as empty boxes.
            The produced document is visibly broken — do not download it as is.`;
  }

  let html = `
    <div class="seal${cls}">
      <div class="seal-score">${score}<span style="font-size:14px">%</span></div>
      <div>
        <div class="seal-label">${label}</div>
        <div class="seal-sub">${note}${pushed && sev !== 'exact'
          ? ` ${pushed} line${plural(pushed)} ${pushed > 1 ? 'were' : 'was'} also pushed on purpose.`
          : ''}${sev === 'drift' ? ' Open the Verify tab to see where.' : ''}</div>
      </div>
    </div>

    <div class="bar">
      <i style="background:var(--acid);width:${(c.inplace || 0) / tot * 100}%"></i>
      <i style="background:var(--blue);width:${recomposed / tot * 100}%"></i>
      <i style="background:var(--red);width:${(c.blocked || 0) / tot * 100}%"></i>
    </div>

    <div class="stat-row"><span>Replaced pixel for pixel</span><span>${c.inplace || 0}</span></div>
    <div class="stat-row"><span>Recomposed with compensation</span><span>${recomposed}</span></div>
    <div class="stat-row"><span>Blocked</span><span>${c.blocked || 0}</span></div>
    <div class="stat-row"><span>Pages verified</span><span>${v ? v.pages.length : 0}</span></div>`;

  const sentence = fitSentence(fits);
  if (sentence) {
    html += `<div class="fit-line">Width handling — ${sentence}.</div>`;
  }

  /* An empty box is the only defect pixel comparison cannot see: it appears
     right where a change is expected. So we show it separately, and loudly. */
  const tofu = out.notdef || [];
  if (tofu.length) {
    const chars = [...new Set(tofu.map(e => e.char))].join(' ');
    const fonts = [...new Set(tofu.map(e => e.font))].join(', ');
    html += `<div class="repair bad">
      <div class="repair-k">⚠ ${tofu.length} character${plural(tofu.length)} with no outline</div>
      <div class="repair-v">The font <span class="mono">${esc(fonts)}</span> declares
        <span class="mono">${esc(chars)}</span> but draws no outline for it:
        those characters would come out as empty boxes. Upload the original font
        file, or uncheck those matches.</div>
    </div>`;
  }

  if (S.docs.length > 1 && (out.perDoc || []).length) {
    html += `<div class="stat-row" style="border-top:1px solid var(--line-soft);margin-top:8px;padding-top:9px">
      <span>Documents changed</span><span>${out.modified}/${out.docs}</span></div>`;
    for (const p of out.perDoc) {
      const bad = p.notdef || p.severity === 'drift';
      html += `<div class="stat-row"><span style="color:${bad ? 'var(--red)' : 'var(--dim)'}">
        ${esc(p.name)}</span><span>${p.applied || '—'}</span></div>`;
    }
    if (out.untouched.length)
      html += `<div class="repair"><div class="repair-v">No match, therefore
        unchanged and left out of the archive:
        ${esc(out.untouched.join(', '))}.</div></div>`;
  }

  for (const r of (out.repairs || [])) {
    html += `<div class="repair${r.ok ? '' : ' bad'}">
      <div class="repair-k">${r.ok ? '✓ Font repaired' : '⚠ Repair impossible'} — ${esc(r.font)}${S.docs.length > 1 && r.doc ? ` <span style="opacity:.7">· ${esc(r.doc)}</span>` : ''}</div>
      <div class="repair-v">${r.ok
        ? `Glyphs ${esc((r.added || []).join(' '))} reinjected from <span class="mono">${esc(r.source || '?')}</span>${r.exact_source ? ' (exact original font)' : ' (close font — check the rendering)'}.`
        : esc(r.reason || 'unknown cause')}</div>
    </div>`;
  }

  const refused = out.refusedList || [];
  if (refused.length) {
    html += `<div class="repair bad"><div class="repair-k">⚠ ${refused.length} blocked</div>
      <div class="repair-v">${refused.slice(0, 4).map(r =>
        `<div><span class="mono">${esc(r.old)}</span>${S.docs.length > 1 && r.doc ? ` <span style="color:var(--dimmer)">(${esc(r.doc)})</span>` : ''} — ${esc(r.reason || '')}</div>`).join('')}
      ${refused.length > 4 ? `<div>… and ${refused.length - 4} more</div>` : ''}</div></div>`;
  }

  $('#reportBody').innerHTML = html;
  $('#reportCard').hidden = false;
}

function updateViewTabs() {
  const d = D();
  const has = !!(d && d.has_output);
  $$('#viewTabs button').forEach(b => {
    const v = b.dataset.view;
    if (v !== 'before') b.disabled = !has;
    b.classList.toggle('on', v === S.view);
  });
}

/* ---------------------------------------------------------------- wiring */

function initRules() {
  const list = $('#ruleList');

  list.addEventListener('input', e => {
    const inp = e.target.closest('input[data-f]');
    if (!inp) return;
    const f = S.finders.find(x => x.id === inp.dataset.rid);
    if (!f) return;
    f[inp.dataset.f] = inp.value;
    refreshMemoryState();
    scheduleScan();
  });

  list.addEventListener('click', e => {
    const tg = e.target.closest('.tg');
    if (tg) {
      const f = S.finders.find(x => x.id === tg.dataset.rid);
      if (!f) return;
      if (tg.dataset.t === 'case') f.case_sensitive = !f.case_sensitive;
      else if (tg.dataset.t === 'word') f.whole_word = !f.whole_word;
      else {
        f.kind = f.kind === 'regex' ? 'text' : 'regex';
        const help = tg.closest('.rule').querySelector('.rule-help');
        if (help) help.hidden = f.kind !== 'regex';
      }
      tg.classList.toggle('on');
      refreshMemoryState();
      scheduleScan();
      return;
    }
    const del = e.target.closest('.rule-del');
    if (del) {
      S.finders = S.finders.filter(x => x.id !== del.dataset.rid);
      if (!S.finders.length) S.finders.push(newRule());
      renderRuleList();
      renderMemory();
      scheduleScan();
    }
  });

  const mem = $('#memList');
  mem.addEventListener('click', e => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'toggle') memToggle(btn.dataset.k);
    else memForget(btn.dataset.k);
  });
  $('#memClear').addEventListener('click', () => {
    if (!memLoad().length) return;
    memWrite([]);
    renderMemory();
    toast('Saved rules cleared.');
  });

  $('#btnAddRule').addEventListener('click', () => {
    S.finders.push(newRule());
    renderRuleList();
    const inputs = $$('#ruleList input[data-f="query"]');
    if (inputs.length) inputs[inputs.length - 1].focus();
  });
}

function initDates() {
  $('#optDates').addEventListener('change', () => {
    S.dates = $('#optDates').checked;
    $('#datesBody').hidden = !S.dates;
    scheduleScan();
  });

  $$('#modeSeg button').forEach(b => b.addEventListener('click', () => {
    S.dateMode = b.dataset.mode;
    $$('#modeSeg button').forEach(x => x.classList.toggle('on', x === b));
    $$('.mode-body').forEach(x => { x.hidden = x.dataset.body !== S.dateMode; });
    scheduleScan();
  }));

  ['Days', 'Months', 'Years'].forEach(k => {
    $('#sh' + k).addEventListener('input', e => {
      S.shift[k.toLowerCase()] = parseInt(e.target.value || '0', 10) || 0;
      scheduleScan();
    });
  });

  $$('#shiftPresets button').forEach(b => b.addEventListener('click', () => {
    S.shift = { days: +b.dataset.d, months: +b.dataset.m, years: +b.dataset.y };
    $('#shDays').value = S.shift.days;
    $('#shMonths').value = S.shift.months;
    $('#shYears').value = S.shift.years;
    scheduleScan();
  }));

  // Mapping targets change the proposed text: the scan computes it, not the
  // simulation.
  $('#mapList').addEventListener('input', e => {
    const i = e.target.closest('input[data-mapkey]');
    if (!i) return;
    const k = i.dataset.mapkey;
    if (i.value) S.mapTargets[k] = i.value; else delete S.mapTargets[k];
    const row = i.closest('.map-row');
    if (row) row.classList.toggle('done', !!i.value);
    scheduleScan();
  });
}

function initOccList() {
  const list = $('#occList');

  list.addEventListener('click', e => {
    const fitBtn = e.target.closest('button[data-fit]');
    if (fitBtn) {
      e.stopPropagation();
      const o = S.occ.find(x => x.id === fitBtn.dataset.oid);
      if (!o) return;
      const v = fitBtn.dataset.fit;
      if (v === 'auto') delete S.fits[sig(o)]; else S.fits[sig(o)] = v;
      const parent = fitBtn.parentElement;
      $$('button[data-fit]', parent).forEach(b => b.classList.toggle('on', b === fitBtn));
      scheduleSim();
      return;
    }
    if (e.target.matches('input')) return;
    const node = e.target.closest('.occ');
    if (node) selectOcc(node.dataset.id);
  });

  list.addEventListener('change', e => {
    const cb = e.target.closest('input[data-check]');
    if (!cb) return;
    const o = S.occ.find(x => x.id === cb.dataset.check);
    if (!o) return;
    if (cb.checked) { S.checked.add(o.id); S.unchecked.delete(sig(o)); }
    else { S.checked.delete(o.id); S.unchecked.add(sig(o)); }
    cb.closest('.occ').classList.toggle('unchecked', !cb.checked);
    $('#occCount').textContent = `${S.checked.size}/${S.occ.length}`;
    drawOverlay();
    scheduleSim();
  });

  list.addEventListener('input', e => {
    const inp = e.target.closest('input[data-ov]');
    if (!inp) return;
    const o = S.occ.find(x => x.id === inp.dataset.ov);
    if (!o) return;
    if (inp.value) S.overrides[sig(o)] = inp.value; else delete S.overrides[sig(o)];
    scheduleSim();
  });
}

function initWorkspace() {
  $('#btnBack').addEventListener('click', () => {
    if (S.sid) fetch(`/api/lots/${S.sid}`, { method: 'DELETE', headers: { 'X-Atelier-Source': 'plateforme' } }).catch(() => {});
    scanToken++; simToken++;
    S.sid = null; S.analysis = null;
    resetSession();
    renderRuleList();
    $('#workspace').hidden = true;
    $('#landing').hidden = false;
    $('#file').value = '';
  });

  initRules();
  initDates();
  initOccList();

  $('#optRepair').addEventListener('change', () => { renderOccList(); renderSummary(); });

  $('#selAll').addEventListener('click', () => {
    S.unchecked = new Set();
    rebuildChecked();
    renderOccList(); drawOverlay(); scheduleSim();
  });
  $('#selNone').addEventListener('click', () => {
    S.unchecked = new Set(S.occ.map(sig));
    S.checked = new Set();
    renderOccList(); drawOverlay(); scheduleSim();
  });
  $('#occFilter').addEventListener('input', debounce(renderOccList, 160));

  $('#pgPrev').addEventListener('click', () => goToPage(S.page - 1));
  $('#pgNext').addEventListener('click', () => goToPage(S.page + 1));

  $$('#viewTabs button').forEach(b => b.addEventListener('click', () => {
    if (b.disabled) return;
    S.view = b.dataset.view;
    updateViewTabs();
    renderPage();
  }));

  $('#btnApply').addEventListener('click', applyEdits);

  $('#btnReset').addEventListener('click', () => {
    S.finders = [newRule()];
    S.dates = false;
    S.mapTargets = {}; S.overrides = {}; S.fits = {};
    S.unchecked = new Set(); S.sim = {}; S.selected = null;
    S.shift = { days: 0, months: 0, years: 0 };
    $('#optDates').checked = false;
    $('#datesBody').hidden = true;
    $('#shDays').value = 0; $('#shMonths').value = 0; $('#shYears').value = 0;
    renderRuleList();
    runScan();
  });

  $('#btnDownload').addEventListener('click', () => {
    window.location = `/api/lots/${S.sid}/download`;
  });

  const fdrop = $('#fontDrop'), ffile = $('#fontFile');
  if (fdrop) {
    fdrop.addEventListener('click', () => ffile.click());
    ffile.addEventListener('change', () => { if (ffile.files[0]) sendFont(ffile.files[0]); });
    ['dragenter', 'dragover'].forEach(ev => fdrop.addEventListener(ev, e => {
      e.preventDefault(); fdrop.classList.add('hot'); }));
    ['dragleave', 'drop'].forEach(ev => fdrop.addEventListener(ev, e => {
      e.preventDefault(); fdrop.classList.remove('hot'); }));
    fdrop.addEventListener('drop', e => {
      const f = e.dataTransfer.files[0]; if (f) sendFont(f); });
  }

  // document rail
  $('#railList').addEventListener('click', e => {
    const del = e.target.closest('[data-del]');
    if (del) { e.stopPropagation(); removeDoc(del.dataset.del); return; }
    const item = e.target.closest('.doc');
    if (item) selectDoc(item.dataset.did);
  });
  const addInput = $('#fileMore'), addLabel = $('#railAdd');
  addInput.addEventListener('change', () => {
    if (addInput.files.length) { addFiles(addInput.files); addInput.value = ''; }
  });
  ['dragenter', 'dragover'].forEach(ev => addLabel.addEventListener(ev, e => {
    e.preventDefault(); addLabel.classList.add('hot'); }));
  ['dragleave', 'drop'].forEach(ev => addLabel.addEventListener(ev, e => {
    e.preventDefault(); addLabel.classList.remove('hot'); }));
  addLabel.addEventListener('drop', e => {
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  $('#btnDownloadOne').addEventListener('click', () => {
    if (S.did) window.location = `/api/lots/${S.sid}/documents/${S.did}/download`;
  });

  let scrollTick = false;
  $('#stageScroll').addEventListener('scroll', () => {
    if (scrollTick) return;
    scrollTick = true;
    requestAnimationFrame(() => {
      scrollTick = false;
      syncPageIndicator();
      placeKnob();
    });
  }, { passive: true });

  // Direct jump: the counter turns into a field when clicked.
  const label = $('#pgLabel'), input = $('#pgInput');
  label.addEventListener('click', () => {
    label.hidden = true; input.hidden = false;
    input.max = String((S.analysis && S.analysis.pages) || 1);
    input.value = String(S.page + 1);
    input.focus(); input.select();
  });
  const closeInput = () => { input.hidden = true; label.hidden = false; };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { goToPage((parseInt(input.value, 10) || 1) - 1); closeInput(); }
    else if (e.key === 'Escape') closeInput();
  });
  input.addEventListener('blur', closeInput);

  initWipe();

  document.addEventListener('keydown', e => {
    if (e.target.matches('input, textarea')) return;
    if (e.key === 'ArrowLeft' || e.key === 'PageUp')    goToPage(S.page - 1);
    if (e.key === 'ArrowRight' || e.key === 'PageDown') goToPage(S.page + 1);
    if (e.key === 'Home') goToPage(0);
    if (e.key === 'End' && S.analysis) goToPage(S.analysis.pages - 1);
  });

  // Resizing changes the display width, hence the useful resolution. We only
  // re-render when the gap is significant: no point rebuilding the image for
  // ten pixels.
  window.addEventListener('resize', debounce(() => {
    if (!S.analysis) return;
    const { cssWidth, dpi } = sheetLayout();
    if (!S.dpi || Math.abs(dpi - S.dpi) / S.dpi > 0.12) { renderPage(); return; }
    for (const el of $$('#pages .page')) {
      const i = +el.dataset.p;
      const size = S.analysis.page_sizes[i] || [595, 842];
      el.style.width = cssWidth + 'px';
      el.style.height = Math.round(cssWidth * size[1] / size[0]) + 'px';
    }
    drawOverlay(); applyWipe();
  }, 200));
}

function initWipe() {
  const scroll = $('#stageScroll'), knob = $('#wipeKnob');
  let dragging = false;

  const move = e => {
    if (!dragging || S.view !== 'compare') return;
    const el = pageEl(S.page) || $('#pages .page');
    if (!el) return;
    const r = el.getBoundingClientRect();
    const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
    S.wipe = Math.max(0, Math.min(100, x / r.width * 100));
    applyWipe();
    if (e.cancelable) e.preventDefault();
  };

  knob.addEventListener('mousedown', e => { dragging = true; e.preventDefault(); });
  scroll.addEventListener('mousedown', e => {
    if (S.view !== 'compare' || !e.target.closest('.page')) return;
    dragging = true; move(e);
  });
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', () => { dragging = false; });
  knob.addEventListener('touchstart', () => { dragging = true; }, { passive: true });
  window.addEventListener('touchmove', move, { passive: false });
  window.addEventListener('touchend', () => { dragging = false; });
}

initLanding();
initWorkspace();
resetSession();
renderRuleList();
