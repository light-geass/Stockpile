// Scratch: verify the run path surfaces the new errors and the UI never hangs. Deleted after use.
export default async function run(page, ui) {
  const out = {};

  // A: FYERS rate-limits the very first call (the "non-JSON response (HTTP 429)" case).
  await page.route('**/api/config', async r => {
    const res = await r.fetch();
    const body = await res.json();
    r.fulfill({ json: { ...body, fyers: { state: 'connected', label: 'Connected', detail: 'stub' } } });
  });
  await page.goto('http://127.0.0.1:5099/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#fyLabel');
  out.pillBefore = (await page.innerText('#fyLabel')).trim();

  await page.click('#go');                                   // Run analysis (universe scan)
  await page.waitForFunction(() => !document.querySelector('#go').textContent.includes('Running'), null, { timeout: 30000 });
  out.afterRun = {
    notice: (await page.innerText('#notices')).replace(/\s+/g, ' ').trim().slice(0, 200),
    button: (await page.innerText('#go')).trim(),
    pillAfter: (await page.innerText('#fyLabel')).trim(),
  };

  // B: the detail panel with an 'unknown' freshness bundle must say so, not claim live/stale.
  const payload = {
    status: 'done', scope: 'focused', target: { symbol: 'WIPRO', name: 'WIPRO LTD', exchange: 'NSE' }, running: false,
    engine: 'deterministic', pulled_at: '30 Oct 2026, 10:00 IST', mode: 'live', message: 'Focused analysis complete.',
    agents: Object.fromEntries(['scout', 'technician', 'fundamentalist', 'newsdesk', 'bull', 'bear', 'judge', 'messenger']
      .map(k => [k, { name: k, role: k, status: 'done', stats: {} }])),
    kpis: { universe: 1, in_debate: 1, buys: 0, top_pick: { symbol: 'WIPRO', confidence: 5, verdict: 'WATCH' } },
    verdicts: [{
      symbol: 'WIPRO', name: 'WIPRO LTD', cap: 'Selected', asset_type: 'EQUITY', verdict: 'WATCH', confidence: 5,
      winner: 'Bull', why: 'RVOL 1.1x.', catalyst: 'n/a', bull: 30, bear: 20, price: 245.1, chg: -0.4, engine: 'deterministic',
      fallback: null, freshness: 'unknown', quote_age: null, gaps: [], alert: { status: 'none' }, guards: [],
      detail: {
        scores: { bull: { score: 30 }, bear: { score: 20 }, technicals: { score: 40 }, fundamentals: { score: null }, news: { score: null } },
        news: [], news_note: null, analyst: {}, range_52w: {}, limitation: ''
      }
    }],
    warnings: [], footer: { text: 'Focused analysis of WIPRO', pulled_at: 'x', synthetic: false },
  };
  await page.route('**/api/status', r => r.fulfill({ json: payload }));
  await page.goto('http://127.0.0.1:5099/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.row .head');
  await page.click('.row .head');
  await page.waitForSelector('.row[data-open=true] .detail');
  out.detail = (await page.innerText('.row .detail')).replace(/\s+/g, ' ').trim();
  out.freshnessShown = out.detail.includes('data: unknown');
  out.scoreChips = (out.detail.match(/News n\/a|Fundamentals n\/a/g) || []);
  return out;
}
