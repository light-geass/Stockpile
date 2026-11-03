/**
 * Signal Desk - Modern Multi-Agent Stock Analysis Dashboard
 * Native ES6+ Modular Event-Driven Frontend Controller
 */

// ============================================================================
// State Store & API Boundary
// ============================================================================

function getInitialTheme() {
  try {
    const saved = localStorage.getItem('sd_theme');
    if (saved === 'light' || saved === 'dark') return saved;
  } catch (_) {}
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'light' || attr === 'dark') return attr;
  if (typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
    return 'light';
  }
  return 'dark';
}

const state = {
  config: null,
  status: null,
  watchlists: [],
  activeWatchlistId: null,
  chosenInstrument: null,
  search: { items: [], activeIndex: -1, querySeq: 0, timer: null },
  workspace: 'analysis',
  isRunning: false,
  pollTimer: null,
  lastAccuracyData: null,
  replay: { busy: false, result: null, symbol: 'NSE:NIFTYBEES-EQ' },
  theme: getInitialTheme()
};

const CSRF = document.querySelector('meta[name="csrf"]')?.content || '';

async function api(path, opt = {}) {
  const method = (opt.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', ...(opt.headers || {}) };
  if (['POST', 'PATCH', 'DELETE'].includes(method)) headers['X-CSRF-Token'] = CSRF;

  const res = await fetch(path, { credentials: 'same-origin', ...opt, headers });
  if (res.status === 401) {
    window.location.href = '/unlock';
    throw new Error('Session locked');
  }
  return res;
}

// ============================================================================
// Utilities & DOM Helper
// ============================================================================

const fmtCurrency = v => (v == null || isNaN(v)) ? 'n/a' : '₹' + Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = v => (v == null || isNaN(v)) ? 'n/a' : (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';
const fmtReturn = v => (v == null || isNaN(v)) ? '–' : (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';
const fmtRate = v => (v == null || isNaN(v)) ? '–' : (Number(v) * 100).toFixed(1) + '%';

function refreshIcons() {
  if (window.lucide?.createIcons) window.lucide.createIcons();
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class' || k === 'className') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k.startsWith('data-') || (v !== false && v != null)) el.setAttribute(k, v);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

function showNotice(level, msg, duration = 0) {
  const box = document.getElementById('notices');
  if (!box) return;
  const b = h('div', { class: `banner ${level}`, role: 'status' }, h('span', {}, msg));
  box.replaceChildren(b);
  if (duration > 0) setTimeout(() => { if (box.contains(b)) b.remove(); }, duration);
}

function applyTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark';
  state.theme = t;
  document.documentElement.setAttribute('data-theme', t);
  document.documentElement.setAttribute('data-color-scheme', t);
  try {
    localStorage.setItem('sd_theme', t);
  } catch (_) {}
  const btn = document.getElementById('themeToggle');
  if (btn) {
    btn.setAttribute('aria-label', t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
    btn.setAttribute('title', t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }
}

// ============================================================================
// Verdicts & Deep Detail Renderers
// ============================================================================

function renderAgents(agents, isFocused) {
  const container = document.getElementById('agents');
  if (!container) return;

  const cards = Object.entries(agents || {}).map(([key, a]) => {
    const role = (isFocused && key === 'scout') ? 'Retrieves the selected instrument' : a.role;
    const stats = Object.entries(a.stats || {});
    const statItems = stats.length
      ? stats.slice(0, 2).map(([lbl, val]) => h('div', { class: 'agent-stat' }, h('small', {}, lbl), h('b', {}, val)))
      : [h('div', { class: 'agent-stat' }, h('small', {}, 'status'), h('b', {}, a.status || 'idle'))];

    return h('article', { class: 'card agent-card', 'data-s': a.status },
      h('div', { class: 'agent-top' },
        h('h3', { class: 'agent-name', title: a.name }, a.name),
        h('span', { class: `status-dot ${a.status || 'offline'}`, title: `Status: ${a.status}` })
      ),
      h('p', { class: 'agent-role', title: role }, role),
      h('div', { class: 'agent-stats' }, statItems)
    );
  });
  container.replaceChildren(...cards);
}

function renderFuturePanel(f) {
  if (!f) return null;
  const p = f.prediction || {}, hs = Object.keys(p), evs = f.events || [], reg = f.regime || {}, er = f.expected_range || {}, sz = f.sizing || {};
  const probs = hs.map(k => h('span', {}, `P(+${k}d up) `, h('b', {}, p[k].p_up == null ? 'n/a' : (p[k].p_up * 100).toFixed(0) + '%'), p[k].calibrated ? ' ✓' : '', ` (N=${p[k].n ?? '–'}) `));
  const range = hs.filter(k => p[k].interval).map(k => `${k}d ${fmtRate(p[k].interval[0])} … ${fmtRate(p[k].interval[1])}`);
  const skill = hs.filter(k => p[k].brier != null).map(k => h('span', {}, `${k}d Brier `, h('b', {}, Number(p[k].brier).toFixed(4)), p[k].baseline_brier != null ? ` vs base ${Number(p[k].baseline_brier).toFixed(4)} ` : ' '));
  const measured = Object.keys(er).map(k => h('span', {}, `${k}d p10 `, h('b', {}, fmtReturn(er[k].p10)), ' med ', h('b', {}, fmtReturn(er[k].median)), ' p90 ', h('b', {}, fmtReturn(er[k].p90)), ` (N=${er[k].n ?? '–'}) `));

  return h('div', { class: 'tag', style: 'margin-top:10px;display:flex;flex-direction:column;gap:5px' },
    probs.length ? h('div', {}, 'Calibrated probability: ', probs) : h('div', {}, 'No probability model yet (needs settled history).'),
    range.length ? h('div', {}, 'Probability interval (base rate ±10pts): ', range.join(' · ')) : null,
    skill.length ? h('div', {}, 'Model skill (Brier vs base): ', skill) : null,
    measured.length ? h('div', {}, 'Expected return range (historical past p10–p90): ', measured) : null,
    h('div', {}, `Regime: ${reg.label || 'unknown'}${reg.why ? ' — ' + reg.why : ''} · entry zone ${(f.entry_zone || []).map(fmtCurrency).join('–') || 'n/a'} · stop ${fmtCurrency(f.stop)}${sz.shares ? ` · size ${sz.shares} sh for ₹${sz.budget || 0}` : ''}`),
    f.next_event ? h('div', {}, 'Next event: ' + f.next_event) : null,
    evs.length ? h('ul', {}, evs.map(e => h('li', {}, `${e.date} · ${e.kind} · ${e.title}`))) : null,
    f.etf ? h('div', {}, `ETF: premium ${fmtPct(f.etf.premium_pct)} (${f.etf.nav_source === 'inav' ? 'live iNAV' : 'EOD NAV'}) · expense ${f.etf.expense_ratio ?? 'n/a'} · tracking diff 20d ${fmtPct(f.etf.tracking_diff_20d)}`) : null,
    f.ipo ? h('div', {}, `IPO: listed ${f.ipo.listing_date || 'n/a'} · vs issue ${fmtPct(f.ipo.vs_issue_pct)}${f.ipo.sme ? ' · SME' : ''}`) : null
  );
}

function renderVerdictRow(v) {
  const d = v.detail || {}, isUp = (v.chg || 0) >= 0, s3 = d.scores3;
  const overall = s3?.overall?.score != null ? s3.overall.score : null;
  const row = h('article', { class: 'card verdict-row', 'data-open': 'false', 'data-symbol': v.symbol });

  const head = h('button', { class: 'verdict-head', type: 'button', 'aria-expanded': 'false', 'data-action': 'toggle-row' },
    h('div', { class: 'sym-col' },
      h('div', { class: 'sym-header' }, h('b', {}, v.symbol), h('span', { class: 'chip' }, v.asset_type || v.cap || 'EQUITY')),
      h('small', {}, v.name)
    ),
    h('div', {},
      h('span', { class: `badge ${v.verdict}` }, v.verdict),
      overall != null ? h('div', { class: 'tag', style: 'margin-top:3px;font-weight:600' }, `Overall ${overall}`) : null
    ),
    h('div', { class: 'conv-col' },
      h('small', {}, `Conviction ${v.confidence}/10 · ${v.winner || 'Debate'} won`),
      h('div', { class: 'progress-track' }, h('span', { class: 'progress-fill', style: `--v:${v.confidence}` }))
    ),
    h('div', { class: 'price-col' },
      h('div', { class: 'price-val' }, fmtCurrency(v.price) + ' ', h('span', { class: isUp ? 'price-up' : 'price-dn' }, (isUp ? '▲ ' : '▼ ') + fmtPct(v.chg))),
      h('div', { class: 'rationale-text' }, v.why)
    ),
    h('span', { class: 'caret-icon', 'aria-hidden': 'true' }, h('i', { 'data-lucide': 'chevron-right' }))
  );

  const scoreMap = { bull: 'Bull', bear: 'Bear', technicals: 'Technicals', fundamentals: 'Fundamentals', news: 'News' };
  const scoreChips = Object.entries(scoreMap).map(([k, lbl]) => h('span', { class: 'score-chip' }, `${lbl} `, h('b', {}, d.scores?.[k]?.score ?? 'n/a')));
  const s3Chips = s3 ? Object.entries({ news_trend: 'News/trend', pattern: 'Pattern', overall: 'Overall' }).map(([k, lbl]) => h('span', { class: 'score-chip' }, `${lbl} `, h('b', {}, s3[k]?.score ?? 'n/a'))) : [];

  const patterns = d.chart?.available
    ? (d.chart.patterns?.length ? d.chart.patterns.map(p => h('li', {}, h('b', {}, p.name.replace(/_/g, ' ') + ': '), `${p.direction} · ${p.status} ${p.levels?.length ? '· level ' + p.levels.map(fmtCurrency).join('/') : ''}`)) : [h('li', {}, 'No reliable pattern identified.')])
    : [h('li', {}, 'Chart data unavailable.')];

  const panelPts = Object.entries(d.scores || {}).map(([k, val]) => {
    const l = val.point ? [val.point] : (val.reasons || []);
    return l.length ? h('li', {}, h('b', {}, (scoreMap[k] || k) + ': '), l.join(' · ')) : null;
  }).filter(Boolean);

  const news = (d.news || []).map(n => h('li', {}, n.url?.startsWith('https://') ? h('a', { href: n.url, target: '_blank', rel: 'noopener' }, n.title) : n.title, ' ', h('span', { class: 'tag' }, `${n.sentiment || ''} ${n.domain ? '· ' + n.domain : ''}`)));
  const an = d.analyst || {};

  const detail = h('div', { class: 'verdict-detail' },
    h('div', { class: 'scores-wrap' }, scoreChips),
    s3Chips.length ? h('div', { class: 'scores-wrap' }, s3Chips) : null,
    h('div', { class: 'detail-grid' },
      h('div', {}, h('h4', {}, 'Panel reasoning'), h('ul', {}, panelPts), h('h4', { style: 'margin-top:14px' }, 'Chart patterns'), h('ul', {}, patterns)),
      h('div', {},
        h('h4', {}, 'Headlines'), news.length ? h('ul', {}, news) : h('p', { class: 'tag' }, 'No relevant headlines found.'),
        h('h4', { style: 'margin-top:14px' }, 'Analyst consensus'),
        h('p', { class: 'tag' }, an.consensus ? `Consensus ${an.consensus} · ${an.num_analysts ?? '?'} analysts · target ${fmtCurrency(an.target_mean)} (${fmtPct(an.upside_pct)})` : (d.analyst_reason || 'Analyst data unavailable.'))
      )
    ),
    renderFuturePanel(v.future),
    v.guards?.length ? h('div', { class: 'tag warnt', style: 'margin-top:6px' }, v.guards.join('; ')) : null,
    v.gaps?.length ? h('div', { class: 'tag warnt' }, 'Data gaps: ' + v.gaps.join(', ')) : null
  );

  row.append(head, detail);
  return row;
}

function renderPlaybook(p) {
  const box = document.getElementById('playbook');
  if (!box) return;
  if (!p?.active || !p.rows?.length) { box.replaceChildren(); return; }

  const rows = p.rows.map(r => h('article', { class: 'card', style: 'padding:14px 18px;margin-bottom:8px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;' },
    h('div', { class: 'sym-col' }, h('b', {}, r.symbol), h('small', {}, r.name || '')),
    h('div', {}, h('span', { class: `badge ${r.verdict}` }, r.verdict), r.eligible ? h('div', { class: 'tag', style: 'font-weight:600' }, 'eligible for close') : null),
    h('div', { class: 'tag', style: 'flex:1 1 200px' }, r.reasons?.join(' · ') || 'clear'),
    h('div', { class: 'tag', style: 'font-family:var(--font-mono)' }, `median ${fmtPct(r.gap_median)} · tail ${fmtPct(r.gap_p5)} · stop ${fmtCurrency(r.stop)}`)
  ));

  box.replaceChildren(
    h('div', { class: 'section-header' }, h('h2', { class: 'section-title' }, h('i', { 'data-lucide': 'moon', class: 'section-title-icon' }), h('span', {}, 'Overnight Playbook')), h('span', { class: 'section-badge' }, 'Near Close (15:00-15:20 IST)')),
    h('p', { class: 'tag', style: 'margin:6px 0 10px' }, `${p.note || ''} ${p.disclaimer || ''}`),
    ...rows
  );
}

// ============================================================================
// Core Dashboard Status & KPIs
// ============================================================================

function renderDashboard(s) {
  state.status = s;
  state.isRunning = Boolean(s.running);
  const isFocused = s.scope === 'focused', idle = s.status === 'idle', k = s.kpis || {};

  const kpis = document.querySelector('.kpis-grid');
  if (kpis) {
    kpis.children[0].querySelector('.kpi-label').textContent = isFocused ? 'Instrument' : (s.watchlist?.name || 'Watchlist');
    kpis.children[0].querySelector('.kpi-caption').textContent = isFocused ? 'selected instrument' : 'stocks scanned';
    kpis.children[1].querySelector('.kpi-caption').textContent = isFocused ? 'analysed' : 'shortlisted by Scout';
    kpis.children[2].querySelector('.kpi-caption').textContent = isFocused ? 'in this instrument' : 'verdict = BUY';
  }

  const k1 = document.getElementById('k1'), k2 = document.getElementById('k2'), k3 = document.getElementById('k3'), k4 = document.getElementById('k4'), k4s = document.getElementById('k4s');
  if (k1) k1.textContent = isFocused ? (s.target?.symbol || '–') : (k.universe || (idle ? '–' : 0));
  if (k2) k2.textContent = idle ? '–' : (k.in_debate ?? '–');
  if (k3) k3.textContent = idle ? '–' : (k.buys ?? '–');
  if (k4) k4.textContent = idle ? '–' : (k.top_pick?.symbol || '–');
  if (k4s) k4s.textContent = (!idle && k.top_pick) ? `${k.top_pick.verdict} · conviction ${k.top_pick.confidence}/10` : 'conviction score';

  renderAgents(s.agents, isFocused);
  renderPlaybook(s.playbook);

  const feed = document.getElementById('feed');
  if (feed) {
    if (s.verdicts?.length) feed.replaceChildren(...s.verdicts.map(renderVerdictRow));
    else if (s.status === 'running') feed.replaceChildren(h('div', { class: 'card empty-state' }, h('i', { 'data-lucide': 'loader', class: 'empty-state-icon animate-spin' }), h('p', {}, 'Autonomous debate in progress…')));
    else feed.replaceChildren(h('div', { class: 'card empty-state' }, h('i', { 'data-lucide': 'inbox', class: 'empty-state-icon' }), h('p', {}, 'No run yet. Press Run analysis to scan.')));
  }

  if (s.status !== 'idle') {
    const banners = [];
    if (s.status === 'error' && s.message) banners.push(h('div', { class: 'banner bad', role: 'alert' }, s.message));
    if (s.warnings?.length) s.warnings.forEach(w => banners.push(h('div', { class: 'banner warn' }, w)));
    if (banners.length) document.getElementById('notices')?.replaceChildren(...banners);
  }

  const foot = document.getElementById('foot');
  if (foot && s.footer) {
    foot.replaceChildren(
      h('span', { class: 'footer-highlight' }, `${s.footer.text} · data pulled ${s.footer.pulled_at || 'n/a'} · engine: ${s.engine || 'deterministic'}`),
      h('span', {}, 'Analysis only. Conviction is an internal score, not a probability of profit. Not investment advice.')
    );
  }

  updateControlsState();
  refreshIcons();
}

function updateControlsState() {
  const cur = state.watchlists.find(w => w.id === state.activeWatchlistId);
  const count = cur?.count || 0, isWl = state.workspace === 'watchlist';
  const qVal = document.getElementById('q')?.value.trim() || '';
  const canAnalyzeSelected = Boolean(state.chosenInstrument || (qVal.length >= 2 && state.search.items.length > 0));

  const runWl = document.getElementById('runWl'), go = document.getElementById('go'), goSel = document.getElementById('goSel'), wlAdd = document.getElementById('wlAdd');
  if (runWl) { runWl.value = state.activeWatchlistId || ''; runWl.disabled = !state.watchlists.length; }
  if (go) {
    go.disabled = state.isRunning || (isWl ? !state.chosenInstrument : count === 0);
    const s = go.querySelector('span'); if (s) s.textContent = state.isRunning ? 'Working…' : (isWl ? 'Add to watchlist' : 'Run analysis');
  }
  if (goSel) {
    goSel.disabled = state.isRunning || !canAnalyzeSelected;
    goSel.hidden = isWl;
  }
  if (wlAdd) wlAdd.hidden = isWl || !state.chosenInstrument || state.activeWatchlistId == null;

  const wlRename = document.getElementById('wlRename'), wlDel = document.getElementById('wlDel');
  if (wlRename) wlRename.disabled = !cur;
  if (wlDel) wlDel.disabled = !cur || state.watchlists.length < 2;
}

// ============================================================================
// Watchlists & Workspace Controller
// ============================================================================

function renderWatchlistView() {
  const cur = state.watchlists.find(w => w.id === state.activeWatchlistId);
  const sel = document.getElementById('wlSelect');
  if (sel) {
    sel.replaceChildren(...state.watchlists.map(w => h('option', { value: w.id }, `${w.name} (${w.count})`)));
    if (cur) sel.value = cur.id;
  }

  const nameEl = document.getElementById('wlViewName'), metaEl = document.getElementById('wlViewMeta');
  if (nameEl) nameEl.textContent = cur ? cur.name : 'No watchlist selected';
  if (metaEl) metaEl.textContent = cur ? `${cur.count} stock${cur.count === 1 ? '' : 's'} in this list. Search above to add instruments.` : 'Create a list to get started.';

  const grid = document.getElementById('wlViewItems');
  if (grid) {
    if (cur?.items?.length) {
      grid.replaceChildren(...cur.items.map(item => h('article', { class: 'card watchlist-stock' },
        h('div', {}, h('b', {}, item.symbol), h('small', {}, item.name)),
        h('button', { class: 'x-remove-btn', type: 'button', 'aria-label': `Remove ${item.symbol}`, 'data-action': 'remove-stock', 'data-symbol': item.provider_symbol }, '✕')
      )));
    } else {
      grid.replaceChildren(h('div', { class: 'card watchlist-empty' }, h('strong', {}, 'Watchlist is empty'), h('span', {}, 'Search a stock above to add it.')));
    }
  }
  updateControlsState();
  refreshIcons();
}

function applyWatchlists(d, nextId) {
  state.watchlists = d.watchlists || [];
  if (nextId != null) state.activeWatchlistId = nextId;
  else if (!state.watchlists.some(w => w.id === state.activeWatchlistId)) {
    state.activeWatchlistId = state.watchlists.length ? state.watchlists[0].id : null;
  }
  const runWl = document.getElementById('runWl');
  if (runWl) {
    runWl.replaceChildren(...(state.watchlists.length ? state.watchlists.map(w => h('option', { value: w.id }, `${w.name} · ${w.count}`)) : [h('option', {}, 'No watchlists')]));
  }
  renderWatchlistView();
}

async function loadWatchlists() {
  try {
    const res = await api('/api/watchlists');
    if (!res.ok) return;
    const d = await res.json();
    applyWatchlists(d);
  } catch (err) { showNotice('bad', `Watchlists unavailable: ${err.message}`); }
}

async function mutateWatchlist(method, path, body) {
  try {
    const res = await api(path, { method, body: body ? JSON.stringify(body) : undefined });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { showNotice('bad', d.message || 'Action failed.'); return null; }
    return d;
  } catch (_) { showNotice('bad', 'Network error.'); return null; }
}

async function addToWatchlist() {
  if (!state.chosenInstrument || state.activeWatchlistId == null) return;
  const sym = state.chosenInstrument.symbol;
  const d = await mutateWatchlist('POST', `/api/watchlists/${state.activeWatchlistId}/items`, { provider_symbol: state.chosenInstrument.provider_symbol });
  const btn = document.getElementById('wlAdd'), go = document.getElementById('go');
  if (d) {
    applyWatchlists(d, state.activeWatchlistId);
    if (btn) btn.textContent = `Added ${sym} ✓`;
    if (go && state.workspace === 'watchlist') go.textContent = `Added ${sym} ✓`;
  }
  setTimeout(() => { if (btn) btn.textContent = '+ Watchlist'; updateControlsState(); }, 1600);
}

function switchWorkspace(mode) {
  state.workspace = mode;
  ['analysisTab', 'watchlistTab', 'accuracyTab'].forEach(id => {
    const tab = document.getElementById(id);
    const isTarget = id === `${mode}Tab`;
    if (tab) { tab.setAttribute('aria-selected', String(isTarget)); tab.setAttribute('tabindex', isTarget ? '0' : '-1'); }
  });
  ['analysisView', 'watchlistView', 'accuracyView'].forEach(id => {
    const p = document.getElementById(id);
    const isTarget = id === `${mode}View`;
    if (p) { p.hidden = !isTarget; p.classList.toggle('active', isTarget); }
  });

  if (mode === 'watchlist') renderWatchlistView();
  if (mode === 'accuracy') loadAccuracy();
  updateControlsState();
}

// ============================================================================
// Search & Auto-Suggest
// ============================================================================

function closeSearchDropdown() {
  state.search.activeIndex = -1;
  const res = document.getElementById('res'), q = document.getElementById('q');
  if (res) { res.hidden = true; }
  if (q) { q.setAttribute('aria-expanded', 'false'); q.removeAttribute('aria-activedescendant'); }
}

function paintSearch(note) {
  const res = document.getElementById('res'), q = document.getElementById('q');
  if (!res || !q) return;
  if (note) {
    res.replaceChildren(h('li', { class: 'note', role: 'presentation' }, note));
  } else {
    res.replaceChildren(...state.search.items.map((item, i) => h('li', {
      id: `opt-${i}`, role: 'option', 'data-action': 'pick-search', 'data-index': i, 'aria-selected': String(i === state.search.activeIndex)
    },
      h('div', { class: 'sym-block' }, h('b', {}, item.symbol), h('small', {}, item.name)),
      h('small', { class: 'chip' }, `${item.exchange || ''} ${item.asset_type || ''}`.trim())
    )));
    if (state.search.activeIndex >= 0) q.setAttribute('aria-activedescendant', `opt-${state.search.activeIndex}`);
  }
  res.hidden = false;
  q.setAttribute('aria-expanded', 'true');
}

function selectInstrument(item) {
  if (!item) return;
  state.chosenInstrument = item;
  const q = document.getElementById('q');
  if (q) {
    q.value = '';
    q.placeholder = `Selected: ${item.symbol} (${item.name})`;
  }
  closeSearchDropdown();
  const pill = document.getElementById('sel');
  if (pill) {
    pill.replaceChildren(
      h('span', { class: 'pill-check' }, '✓'),
      h('b', {}, item.symbol),
      h('small', {}, `${item.name} · ${item.exchange}`),
      h('button', { type: 'button', 'aria-label': 'Clear selection', 'data-action': 'clear-selection', title: 'Clear selection' }, '✕')
    );
    pill.hidden = false;
  }
  updateControlsState();
}

async function executeSearch() {
  const q = document.getElementById('q');
  const query = q?.value.trim() || '';
  const seq = ++state.search.querySeq;
  if (query.length < 2) { closeSearchDropdown(); return; }

  paintSearch('Searching…');
  try {
    const res = await api(`/api/instruments/search?q=${encodeURIComponent(query)}`);
    if (seq !== state.search.querySeq) return;
    if (res.status === 503) { paintSearch('Symbol catalog warming up…'); return; }
    if (res.status === 429) { paintSearch('Rate limited. Please wait.'); return; }
    if (!res.ok) { paintSearch('Search failed.'); return; }

    const d = await res.json();
    if (seq !== state.search.querySeq) return;
    state.search.items = d.results || [];
    state.search.activeIndex = -1;
    updateControlsState();
    if (!state.search.items.length) { paintSearch('No matching instruments.'); return; }
    paintSearch();
  } catch (_) { if (seq === state.search.querySeq) paintSearch('Network error.'); }
}

// ============================================================================
// Accuracy & Replay Backtest
// ============================================================================

function accTable(title, rows, firstField) {
  if (!rows?.length) return h('div', {}, h('h3', { style: 'font-size:15px;font-weight:700;margin-bottom:6px' }, title), h('p', { class: 'tag' }, 'No settled verdicts yet.'));
  const header = h('tr', { class: 'tag' }, h('th', {}, firstField), h('th', {}, 'N'), h('th', {}, 'Hit rate'), h('th', {}, 'Median'), h('th', {}, 'Mean'), h('th', {}, 'p10'), h('th', {}, 'p90'), h('th', {}, 'Median MAE'));
  const body = rows.map(r => h('tr', {},
    h('td', {}, r[firstField]), h('td', {}, r.n), h('td', {}, fmtRate(r.hit_rate)), h('td', {}, fmtReturn(r.median_return)),
    h('td', {}, fmtReturn(r.mean_return)), h('td', {}, fmtReturn(r.p10)), h('td', {}, fmtReturn(r.p90)), h('td', {}, fmtReturn(r.median_mae))
  ));
  return h('div', {}, h('h3', { style: 'font-size:15px;font-weight:700;margin-bottom:6px' }, title), h('div', { class: 'table-responsive' }, h('table', { class: 'acc' }, header, ...body)));
}

function renderAccuracy(d) {
  state.lastAccuracyData = d;
  const byH = d.horizons.map(k => ({ h: `${k}d`, ...(d.overall?.[k] || { n: 0 }) }));
  const slices = {};
  (d.slices || []).forEach(s => {
    const k = `${s.verdict} · conf ${s.confidence_bucket}`;
    (slices[k] || (slices[k] = [])).push({ h: `${s.horizon}d`, ...s });
  });

  const rep = state.replay.result;
  const repHorizons = rep ? ((rep.horizons) || Object.keys(rep.overall || {})) : [];
  const repTable = rep ? h('div', { class: 'table-responsive', style: 'margin-top:10px' },
    h('table', { class: 'acc' },
      h('tr', { class: 'tag' }, h('th', {}, 'Horizon'), h('th', {}, 'N'), h('th', {}, 'Hit rate'), h('th', {}, 'Median'), h('th', {}, 'Mean'), h('th', {}, 'p10'), h('th', {}, 'p90'), h('th', {}, 'Median MAE')),
      ...repHorizons.filter(k => rep.overall?.[k]).map(k => h('tr', {},
        h('td', {}, `${k}d`), h('td', {}, rep.overall[k].n), h('td', {}, fmtRate(rep.overall[k].hit_rate)), h('td', {}, fmtReturn(rep.overall[k].median_return)),
        h('td', {}, fmtReturn(rep.overall[k].mean_return)), h('td', {}, fmtReturn(rep.overall[k].p10)), h('td', {}, fmtReturn(rep.overall[k].p90)), h('td', {}, fmtReturn(rep.overall[k].median_mae))
      ))
    )
  ) : null;

  const repCard = h('article', { class: 'card', style: 'padding:18px 22px;margin-bottom:14px' },
    h('h3', { style: 'font-size:15px;font-weight:700;margin-bottom:4px' }, 'Replay Accelerator'),
    h('p', { class: 'tag', style: 'margin-bottom:4px' }, "Backtest deterministic engine over one instrument's last year of candles."),
    h('p', { class: 'tag warnt', style: 'margin-bottom:10px' }, rep?.limitation || "Point-in-time gaps apply."),
    h('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px' },
      h('input', { id: 'replaySym', type: 'text', value: state.replay.symbol, style: 'flex:1 1 220px;padding:8px 12px;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface);color:var(--text-primary);font:inherit;' }),
      h('button', { class: 'btn btn-ghost', type: 'button', 'data-action': 'run-replay', disabled: state.replay.busy }, state.replay.busy ? 'Replaying…' : 'Run replay')
    ),
    repTable
  );

  const cards = [
    h('article', { class: 'card', style: 'padding:18px 22px;margin-bottom:14px' },
      h('p', { style: 'font-weight:600;margin-bottom:4px' }, d.disclaimer),
      h('p', { class: 'tag' }, `Settled rows: ${d.n_settled} · Benchmark: ${d.benchmark_symbol} (${d.benchmark_source || 'unrecorded'}) · ${d.survivorship}`)
    ),
    h('article', { class: 'card', style: 'padding:18px 22px;margin-bottom:14px' }, accTable('All verdicts by horizon', byH, 'h')),
    ...Object.entries(slices).map(([k, rows]) => h('article', { class: 'card', style: 'padding:18px 22px;margin-bottom:14px' }, accTable(k, rows, 'h'))),
    d.rolling?.length ? h('article', { class: 'card', style: 'padding:18px 22px;margin-bottom:14px' }, accTable('Rolling monthly cohorts', d.rolling, 'month')) : null,
    repCard
  ].filter(Boolean);

  document.getElementById('accBody')?.replaceChildren(...cards);
  refreshIcons();
}

async function loadAccuracy() {
  try {
    const res = await api('/api/accuracy');
    const d = await res.json();
    renderAccuracy(d);
  } catch (_) {
    document.getElementById('accBody')?.replaceChildren(h('div', { class: 'card empty-state' }, h('p', {}, 'Could not load accuracy history.')));
  }
}

async function runReplay() {
  const sym = document.getElementById('replaySym')?.value.trim();
  if (!sym) { showNotice('warn', 'Enter a FYERS symbol (e.g. NSE:TCS-EQ).', 3000); return; }
  state.replay.symbol = sym; state.replay.busy = true;
  if (state.lastAccuracyData) renderAccuracy(state.lastAccuracyData);

  try {
    const res = await api(`/api/replay?provider_symbol=${encodeURIComponent(sym)}`);
    const d = await res.json();
    if (!res.ok) { showNotice('warn', d.message || 'Replay failed.', 4000); }
    else { state.replay.result = d; showNotice('good', `Replay done: ${d.verdicts} verdicts for ${d.symbol}.`, 4000); }
  } catch (_) { showNotice('bad', 'Replay request failed.', 4000); }

  state.replay.busy = false;
  loadAccuracy();
}

// ============================================================================
// Polling & Config Loader
// ============================================================================

async function fetchStatusTick() {
  try {
    const res = await api('/api/status');
    const d = await res.json();
    renderDashboard(d);
    if (!d.running && state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      loadConfig();
      loadWatchlists();
    }
  } catch (_) {}
}

function startPolling() {
  if (!state.pollTimer) state.pollTimer = setInterval(fetchStatusTick, 700);
  fetchStatusTick();
}

async function loadConfig() {
  try {
    const res = await api('/api/config');
    const c = await res.json();
    state.config = c;
    const f = c.fyers || {};
    const fyDot = document.getElementById('fyDot'), fyLabel = document.getElementById('fyLabel'), fyBtn = document.getElementById('fyBtn');
    if (fyDot) fyDot.className = `status-dot ${f.state || 'offline'}`;
    if (fyLabel) { fyLabel.textContent = `FYERS · ${f.label || 'Offline'}`; fyLabel.title = f.detail || ''; }
    if (fyBtn) { fyBtn.textContent = f.state === 'connected' ? 'Reconnect' : 'Connect FYERS'; fyBtn.disabled = f.state === 'not_configured'; }
    if (c.notice) showNotice(c.notice.level, c.notice.text);

    const auth = new URLSearchParams(window.location.search).get('auth');
    if (auth) {
      const nr = await api(`/api/auth-notice/${encodeURIComponent(auth)}`).then(r => r.json()).catch(() => ({}));
      if (nr.level && nr.text) showNotice(nr.level, nr.text);
      window.history.replaceState(null, '', '/');
    }
    const sub = document.getElementById('sub');
    if (sub) sub.textContent = `Indian stock analysis · 9 agents on duty · ${c.groq ? 'LLM debate active' : 'deterministic engine'}`;
  } catch (_) {}
}

// ============================================================================
// Global Event Delegation
// ============================================================================

function setupEvents() {
  // Global click delegator
  document.addEventListener('click', async e => {
    const target = e.target;

    // Theme toggle
    if (target.closest('#themeToggle')) {
      const current = document.documentElement.getAttribute('data-theme') || state.theme || 'dark';
      applyTheme(current === 'light' ? 'dark' : 'light');
      return;
    }

    // FYERS login
    if (target.closest('#fyBtn')) {
      if (state.config?.tunnel && !state.config.on_tunnel_host) {
        showNotice('warn', `Open dashboard at https://${state.config.redirect_host} to connect FYERS.`);
        return;
      }
      window.location.href = '/auth/fyers/login';
      return;
    }

    // Toggle verdict row detail
    const rowToggle = target.closest('[data-action="toggle-row"]');
    if (rowToggle) {
      const row = rowToggle.closest('.verdict-row');
      const o = row.dataset.open !== 'true';
      row.dataset.open = String(o);
      rowToggle.setAttribute('aria-expanded', String(o));
      return;
    }

    // Pick search suggestion
    const pickSearch = target.closest('[data-action="pick-search"]');
    if (pickSearch) {
      const i = parseInt(pickSearch.dataset.index, 10);
      if (state.search.items[i]) {
        selectInstrument(state.search.items[i]);
      }
      return;
    }

    // Clear search selection
    if (target.closest('[data-action="clear-selection"]')) {
      state.chosenInstrument = null;
      const pill = document.getElementById('sel');
      if (pill) { pill.hidden = true; pill.replaceChildren(); }
      const q = document.getElementById('q');
      if (q) {
        q.value = '';
        q.placeholder = 'Search a stock / ETF / listed IPO (e.g. TCS, NIFTYBEES, ZOMATO)…';
        q.focus();
      }
      updateControlsState();
      return;
    }

    // Close search dropdown on click outside
    if (!target.closest('.search-box')) {
      closeSearchDropdown();
    }

    // Workspace tab switch
    const tab = target.closest('.workspace-tab');
    if (tab) {
      switchWorkspace(tab.id.replace('Tab', ''));
      return;
    }

    // Remove stock from watchlist
    const rmBtn = target.closest('[data-action="remove-stock"]');
    if (rmBtn && state.activeWatchlistId) {
      const d = await mutateWatchlist('DELETE', `/api/watchlists/${state.activeWatchlistId}/items/${encodeURIComponent(rmBtn.dataset.symbol)}`);
      if (d) applyWatchlists(d, state.activeWatchlistId);
      return;
    }

    // Run replay
    if (target.closest('[data-action="run-replay"]')) {
      runReplay();
      return;
    }

    // New Watchlist
    if (target.closest('#wlNew')) {
      const name = window.prompt('Name for the new watchlist:', `Watchlist ${state.watchlists.length + 1}`);
      if (!name?.trim()) return;
      const d = await mutateWatchlist('POST', '/api/watchlists', { name: name.trim() });
      if (d?.watchlist) applyWatchlists(d, d.watchlist.id);
      return;
    }

    // Rename Watchlist
    if (target.closest('#wlRename')) {
      const cur = state.watchlists.find(w => w.id === state.activeWatchlistId);
      if (!cur) return;
      const name = window.prompt('Rename watchlist:', cur.name);
      if (!name?.trim() || name === cur.name) return;
      const d = await mutateWatchlist('PATCH', `/api/watchlists/${cur.id}`, { name: name.trim() });
      if (d) applyWatchlists(d, state.activeWatchlistId);
      return;
    }

    // Delete Watchlist
    if (target.closest('#wlDel')) {
      const cur = state.watchlists.find(w => w.id === state.activeWatchlistId);
      if (!cur || state.watchlists.length < 2) return;
      if (!window.confirm(`Delete "${cur.name}"?`)) return;
      const idx = state.watchlists.indexOf(cur);
      const nextId = (state.watchlists[idx + 1] || state.watchlists[idx - 1]).id;
      const d = await mutateWatchlist('DELETE', `/api/watchlists/${cur.id}`);
      if (d) applyWatchlists(d, nextId);
      return;
    }

    // Add selected to watchlist
    if (target.closest('#wlAdd')) {
      addToWatchlist();
      return;
    }

    // Analyze selected instrument
    if (target.closest('#goSel')) {
      let inst = state.chosenInstrument;
      if (!inst) {
        if (state.search.items.length > 0) {
          inst = state.search.items[0];
          selectInstrument(inst);
        } else {
          const qVal = document.getElementById('q')?.value.trim();
          if (qVal && qVal.length >= 2) {
            try {
              const r = await api(`/api/instruments/search?q=${encodeURIComponent(qVal)}`);
              const d = await r.json();
              if (d.results?.length) {
                inst = d.results[0];
                selectInstrument(inst);
              }
            } catch (_) {}
          }
        }
      }
      if (!inst) {
        showNotice('warn', 'Please search and select a stock to analyze.', 3000);
        return;
      }
      const res = await api('/api/analyze-selected', { method: 'POST', body: JSON.stringify({ provider_symbol: inst.provider_symbol }) });
      if (res.status === 409) { showNotice('warn', 'Run already in progress.', 3000); return; }
      if (!res.ok) { showNotice('bad', 'Targeted run failed.', 4000); return; }
      startPolling();
      return;
    }

    // Run analysis button
    if (target.closest('#go')) {
      if (state.workspace === 'watchlist') { addToWatchlist(); return; }
      if (state.activeWatchlistId == null) return;
      const res = await api('/api/start', { method: 'POST', body: JSON.stringify({ mode: 'live', watchlist_id: state.activeWatchlistId }) });
      if (res.status === 409) { showNotice('warn', 'Run already in progress.', 3000); return; }
      if (!res.ok) { showNotice('bad', 'Could not start analysis.', 4000); return; }
      startPolling();
      return;
    }

    // Accuracy buttons
    if (target.closest('#settleBtn')) {
      const res = await api('/api/outcomes/settle', { method: 'POST', body: '{}' });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) showNotice('warn', d.message || 'Settlement failed.', 4000);
      else { showNotice('good', `Settled ${d.settled} verdict(s).`, 4000); loadAccuracy(); }
      return;
    }

    if (target.closest('#retrainBtn')) {
      const res = await api('/api/model/retrain', { method: 'POST', body: '{}' });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) showNotice('warn', d.message || 'Retraining failed.', 4000);
      else { showNotice('good', d.published?.length ? `Model shipped for ${d.published.join(', ')}d.` : 'Trials ledgered.', 4000); loadAccuracy(); }
      return;
    }

    if (target.closest('#accReload')) {
      loadAccuracy();
      return;
    }
  });

  // Watchlist Select Changes
  document.getElementById('runWl')?.addEventListener('change', e => {
    state.activeWatchlistId = e.target.value;
    renderWatchlistView();
    updateControlsState();
  });

  document.getElementById('wlSelect')?.addEventListener('change', e => {
    state.activeWatchlistId = e.target.value;
    renderWatchlistView();
    updateControlsState();
  });

  // Search dropdown listeners to prevent focus loss & blur race condition
  const resList = document.getElementById('res');
  if (resList) {
    resList.addEventListener('mousedown', e => {
      e.preventDefault(); // Keep input focused, preventing blur timer from firing
    });
    const pickHandler = e => {
      const pick = e.target.closest('[data-action="pick-search"]');
      if (pick) {
        const i = parseInt(pick.dataset.index, 10);
        if (state.search.items[i]) {
          selectInstrument(state.search.items[i]);
        }
      }
    };
    resList.addEventListener('pointerdown', pickHandler);
  }

  // Search input events
  const q = document.getElementById('q');
  if (q) {
    q.addEventListener('input', () => {
      if (state.chosenInstrument) {
        state.chosenInstrument = null;
        const pill = document.getElementById('sel');
        if (pill) { pill.hidden = true; pill.replaceChildren(); }
      }
      updateControlsState();
      clearTimeout(state.search.timer);
      state.search.timer = setTimeout(executeSearch, 200);
    });

    q.addEventListener('keydown', e => {
      if (e.key === 'Escape') { closeSearchDropdown(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!state.search.items.length) return;
        e.preventDefault();
        const d = e.key === 'ArrowDown' ? 1 : -1;
        state.search.activeIndex = (state.search.activeIndex + d + state.search.items.length) % state.search.items.length;
        paintSearch();
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        const pick = state.search.activeIndex >= 0
          ? state.search.items[state.search.activeIndex]
          : state.search.items[0];
        if (pick) {
          selectInstrument(pick);
        } else if (state.chosenInstrument) {
          document.getElementById('goSel')?.click();
        }
        return;
      }
    });

    q.addEventListener('blur', () => {
      setTimeout(() => {
        if (!document.activeElement?.closest('.search-box')) {
          closeSearchDropdown();
        }
      }, 250);
    });
  }
}

// ============================================================================
// Bootstrapper
// ============================================================================

async function init() {
  applyTheme(state.theme);
  setupEvents();
  await loadConfig();
  await loadWatchlists();
  await fetchStatusTick();
  refreshIcons();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
