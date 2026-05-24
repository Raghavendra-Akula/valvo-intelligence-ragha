"""Apply 2026_05_11_stock_universe_ipo_flag.sql and verify backfill."""
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

import psycopg2
import psycopg2.extras

MIGRATION = BACKEND_DIR / "database" / "migrations" / "2026_05_11_stock_universe_ipo_flag.sql"

conn = psycopg2.connect(
    host=os.getenv("DB_HOST"),
    database=os.getenv("DB_NAME", "postgres"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    port=int(os.getenv("DB_PORT", "5432")),
)
conn.autocommit = False
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print(f"Applying {MIGRATION.name}...")
sql = MIGRATION.read_text()
cur.execute(sql)
conn.commit()
print("  ✅ Migration applied.")

# Verify columns exist
cur.execute("""
    SELECT column_name, data_type, column_default
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='stock_universe'
      AND column_name IN ('is_ipo','listing_date')
    ORDER BY column_name
""")
print("\nNew columns:")
for r in cur.fetchall():
    print(f"  {r['column_name']:15s} {r['data_type']:10s} default={r['column_default']}")

# Verify backfill
cur.execute("""
    SELECT symbol, company_name, listing_date, is_ipo
    FROM stock_universe
    WHERE is_ipo = true
    ORDER BY listing_date DESC NULLS LAST
""")
rows = cur.fetchall()
print(f"\nStocks now flagged is_ipo=true: {len(rows)}")
for r in rows:
    print(f"  {str(r['listing_date']):12s} | {r['symbol']:15s} | {(r['company_name'] or '')[:60]}")

# Confirm phantoms are NOT flagged
cur.execute("""
    SELECT COUNT(*) AS cnt FROM stock_universe
    WHERE symbol IN ('FRONTSP','SINGERIND','MARSONS','BI','COCKERILL','NIMBSPROJ','POWERICA')
      AND is_ipo = true
""")
phantom_flagged = cur.fetchone()['cnt']
print(f"\nKnown phantoms flagged as is_ipo (should be 0): {phantom_flagged}")

# Confirm Query B simulation returns ONLY genuine IPOs
print("\nSimulating IPO Lab Query B (Fresh IPOs)...")
cur.execute("""
    WITH missing_sids AS (
        SELECT u.security_id
        FROM stock_universe u
        WHERE u.is_active = true
          AND COALESCE(u.is_etf, false) = false
          AND u.is_ipo = true
          AND NOT EXISTS (SELECT 1 FROM stock_daily_summary s WHERE s.security_id = u.security_id)
    ),
    fresh AS (
        SELECT cd.security_id, MIN(cd.date) as listing_date, COUNT(*) as days
        FROM candles_daily cd
        JOIN missing_sids ms ON cd.security_id = ms.security_id
        WHERE cd.volume > 0
        GROUP BY cd.security_id
        HAVING MIN(cd.date) >= CURRENT_DATE - 365 AND COUNT(*) < 50
    )
    SELECT u.symbol, u.company_name, f.listing_date, f.days
    FROM fresh f
    JOIN stock_universe u USING(security_id)
    ORDER BY f.listing_date DESC
""")
fresh_rows = cur.fetchall()
print(f"  Fresh IPOs returned: {len(fresh_rows)}")
for r in fresh_rows:
    print(f"  {str(r['listing_date'])} | {r['symbol']:15s} | {r['days']:3d}d | {r['company_name']}")

cur.close()
conn.close()
print("\n✅ Done.")
