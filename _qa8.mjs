// Scratch: assert the analyst line renders for a covered stock. Deleted after use.
export default async function run(page, ui) {
  await page.route('**/api/status', r => r.fulfill({
    json: {
      status: 'done', scope: 'focused', target: { symbol: 'ITC' }, running: false, engine: 'deterministic',
      pulled_at: 'x', mode: 'live', message: 'done',
      agents: Object.fromEntries(['scout', 'technician', 'fundamentalist', 'newsdesk', 'bull', 'bear', 'judge', 'messenger']
        .map(k => [k, { name: k, role: k, status: 'done', stats: {} }])),
      kpis: { universe: 1, in_debate: 1, buys: 0, top_pick: null },
      verdicts: [{
        symbol: 'ITC', name: 'ITC LTD', verdict: 'WATCH', confidence: 5, winner: 'Bear', why: 'x', bull: 34, bear: 40,
        price: 318.0, chg: 0.5, engine: 'deterministic', freshness: 'unknown', gaps: [], alert: { status: 'none' },
        detail: {
          scores: {}, news: [], analyst: { consensus: 'buy', num_analysts: 33, buy_pct: 80, target_mean: 324.73, upside_pct: 2.1 },
          analyst_reason: 'fetched', limitation: 'Analyst targets only.'
        }
      }],
      warnings: [], footer: { text: 'x', pulled_at: 'x', synthetic: false },
    }
  }));
  await page.goto('http://127.0.0.1:5099/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.row .head');
  await page.click('.row .head');
  await page.waitForSelector('.row[data-open=true] .detail');
  const d = (await page.innerText('.row .detail')).replace(/\s+/g, ' ').trim();
  return { analystRendered: d.includes('Consensus buy'), contains33: d.includes('33'), snippet: d.slice(d.indexOf('Analyst data'), d.indexOf('Analyst data') + 120) };
}
