// Scratch: Fundamentalist card for a covered stock via a real focused run (stubbed services). Deleted after use.
export default async function run(page, ui) {
  // Serve a config that says FYERS is connected; the run itself will still abort on the real token,
  // so instead stub /api/status with the shape a *successful* covered run produces.
  const agents = {
    scout: { name: 'Scout', role: 'Retrieves the selected instrument', status: 'done', stats: { scanned: 1, selected: 'ITC' } },
    technician: { name: 'Technician', role: 'RVOL, trend, 52-week range', status: 'done', stats: { analyzed: 1, RVOL: '0.70x' } },
    fundamentalist: { name: 'Fundamentalist', role: 'Analyst consensus and targets', status: 'done', stats: { covered: '1/1', 'avg upside': '+2.1%' } },
    newsdesk: { name: 'Newsdesk', role: 'Fresh headlines and tone', status: 'done', stats: { headlines: 12, tone: '4+ / 0-' } },
    bull: { name: 'Bull', role: 'Argues the case to buy', status: 'done', stats: {} },
    bear: { name: 'Bear', role: 'Argues the case against', status: 'done', stats: {} },
    judge: { name: 'Judge', role: 'Weighs both, calls the verdict', status: 'done', stats: {} },
    messenger: { name: 'Messenger', role: 'Sends BUY alerts', status: 'done', stats: {} },
  };
  await page.route('**/api/status', r => r.fulfill({
    json: {
      status: 'done', scope: 'focused', target: { symbol: 'ITC', name: 'ITC LTD', exchange: 'NSE' }, running: false,
      engine: 'deterministic', pulled_at: 'x', mode: 'live', message: 'done', agents,
      kpis: { universe: 1, in_debate: 1, buys: 0, top_pick: { symbol: 'ITC', confidence: 5, verdict: 'WATCH' } },
      verdicts: [{
        symbol: 'ITC', name: 'ITC LTD', cap: 'Selected', verdict: 'WATCH', confidence: 5, winner: 'Bear',
        why: 'x', catalyst: '', bull: 34, bear: 40, price: 318.0, chg: 0.5, engine: 'deterministic', fallback: null,
        freshness: 'unknown', quote_age: null, gaps: [], alert: { status: 'none' }, guards: [],
        detail: { scores: {}, news: [], analyst: { consensus: 'buy', num_analysts: 33, buy_pct: 80, target_mean: 324.73, upside_pct: 2.1 }, analyst_reason: 'fetched', limitation: '' }
      }],
      warnings: [], footer: { text: 'x', pulled_at: 'x', synthetic: false },
    }
  }));
  await page.goto('http://127.0.0.1:5099/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.agent');
  const cards = await page.$$eval('.agent', els => els.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
  await page.click('.row .head');
  await page.waitForSelector('.row[data-open=true] .detail');
  const detail = (await page.innerText('.row .detail')).replace(/\s+/g, ' ');
  return {
    fundamentalistCard: cards.find(c => c.startsWith('Fundamentalist')),
    analystLine: (detail.match(/Consensus[^.]*\.[^.]*\)/) || ['MISSING'])[0],
    saysUnavailable: /Analyst data unavailable/.test(detail),
  };
}
