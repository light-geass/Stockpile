"""One-off probe of the exact add_item INSERT against the live DB."""
import sys

import psycopg

from watchlists import dsn_uri

SQL = ("INSERT INTO watchlist_items(watchlist_id, provider_symbol, symbol, name, exchange, asset_type, position) "
       "SELECT %s, %s, %s, %s, %s, %s, COALESCE(MAX(position)+1, 0) FROM watchlist_items WHERE watchlist_id=%s")

with psycopg.connect(dsn_uri(sys.argv[1]), autocommit=True, connect_timeout=15) as conn:
    ids = [r[0] for r in conn.execute("SELECT id FROM watchlists ORDER BY position, id").fetchall()]
    print("list ids:", ids)
    wid = str(ids[0])
    try:
        conn.execute(SQL, (wid, "NSE:PROBE-EQ", "PROBE", "Probe", "NSE", "EQUITY", wid))
        print("INSERT OK")
        conn.execute("DELETE FROM watchlist_items WHERE provider_symbol=%s", ("NSE:PROBE-EQ",))
        print("cleaned up")
    except Exception as e:
        print("FAIL:", type(e).__name__, e)
