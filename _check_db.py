"""One-off schema inspection against the live Supabase database."""
import os
import sys

import psycopg

from watchlists import dsn_uri

sql = """
SELECT table_name, column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_name IN ('watchlists', 'watchlist_items')
ORDER BY table_name, ordinal_position
"""
with psycopg.connect(dsn_uri(sys.argv[1]), connect_timeout=15) as conn:
    for row in conn.execute(sql).fetchall():
        print(row)
