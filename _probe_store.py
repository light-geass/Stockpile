"""One-off probe of the full WatchlistStore.add_item path against the live DB."""
import logging

import psycopg

import config
from watchlists import WatchlistStore

logging.basicConfig(level=logging.DEBUG)

s = config.Settings.from_env()
st = WatchlistStore(s.database_url, s.watchlist_max_items)
st.connect()
print("connected:", st.pool is not None, "error:", st.error)
lists = st.lists()
print("lists:", [(w["id"], w["name"], w["count"]) for w in lists])
wid = lists[0]["id"]
out = st.add_item(wid, {"provider_symbol": "NSE:PROBE-EQ", "symbol": "PROBE", "name": "Probe",
                        "exchange": "NSE", "asset_type": "EQUITY"})
print("add OK, count:", out["count"])
st.remove_item(wid, "NSE:PROBE-EQ")
print("cleaned up")
