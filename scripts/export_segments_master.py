"""Export the master segment/YoY sheet for all active stocks → CSV.

One-off helper for the theme-classification project. Pulls each stock's top-4
business segments (≥20% of revenue, latest quarter) along with quarter-count-
based TTM YoY growth per segment, plus market cap and existing sector labels.

Output: docs/stock_segments_master.csv
"""

import csv
import os
import sys
from pathlib import Path

# Path setup so we pick up Backend/.env
HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(BACKEND / ".env")


QUERY = r"""
WITH clean_segments AS (
  SELECT DISTINCT ON (s.security_id, s.period_end_date, COALESCE(s.segment_canonical_key, lower(trim(s.segment_name))))
         s.security_id, s.period_end_date,
         COALESCE(s.segment_canonical_key, lower(trim(s.segment_name))) AS seg_key,
         s.segment_name, s.segment_revenue_cr, s.segment_revenue_pct
    FROM segments_quarterly s
   WHERE s.segment_revenue_cr > 0
     AND lower(trim(s.segment_name)) !~ '^(india|europe|america|americas|asia|china|japan|uk|usa|u\.s\.a\.?|united states|united kingdom|middle east|africa|domestic|international|overseas|oversease|oveseas|ovrseas|exports?|rest of[- ]the[- ]world|row|total|segment revenue|others?|other|unallocated|single segment|standalone|consolidated|geographic|geography|north america|south america|emea|apac|india operations|foreign|uk & europe|asia pacific|gulf|scandinavia|latam)$'
     AND lower(s.segment_name) !~ '(geograph|intersegment|intra-segment|outside india|within india|all other|no reportable|single reportable|one segment|rest of world|ovese|overse|& rest)'
   ORDER BY s.security_id, s.period_end_date, COALESCE(s.segment_canonical_key, lower(trim(s.segment_name))), s.is_consolidated DESC NULLS LAST
),
seg_ranked AS (
  SELECT cs.*, ROW_NUMBER() OVER (PARTITION BY cs.security_id, cs.seg_key ORDER BY cs.period_end_date DESC) AS q_rank
    FROM clean_segments cs
),
seg_ttm AS (
  SELECT security_id, seg_key,
         SUM(segment_revenue_cr) FILTER (WHERE q_rank BETWEEN 1 AND 4) AS ttm_r,
         SUM(segment_revenue_cr) FILTER (WHERE q_rank BETWEEN 5 AND 8) AS ttm_p,
         COUNT(*) FILTER (WHERE q_rank BETWEEN 1 AND 4) AS qc_r,
         COUNT(*) FILTER (WHERE q_rank BETWEEN 5 AND 8) AS qc_p
    FROM seg_ranked GROUP BY security_id, seg_key
),
stock_last_qtr AS (
  SELECT security_id, MAX(period_end_date) AS latest_date FROM clean_segments GROUP BY security_id
),
latest_segs AS (
  SELECT sr.security_id, sr.seg_key, sr.segment_name, sr.segment_revenue_cr, sr.segment_revenue_pct,
         ROW_NUMBER() OVER (PARTITION BY sr.security_id ORDER BY sr.segment_revenue_cr DESC NULLS LAST) AS rk
    FROM seg_ranked sr
    JOIN stock_last_qtr slq ON sr.security_id = slq.security_id AND sr.period_end_date = slq.latest_date
),
has_any AS (SELECT DISTINCT security_id FROM segments_quarterly),
has_clean AS (SELECT DISTINCT security_id FROM clean_segments),
mcap_j AS (
  SELECT u.security_id, COALESCE(fo.market_cap_cr, bcm.market_cap_cr) AS mcap_cr
    FROM stock_universe u
    LEFT JOIN fundamentals_overview fo ON fo.security_id = u.security_id
    LEFT JOIN bse_company_master bcm ON bcm.security_id = u.security_id
),
major_count AS (
  SELECT security_id, COUNT(*) AS num_major FROM latest_segs WHERE segment_revenue_pct >= 20 GROUP BY security_id
)
SELECT u.symbol,
       u.company_name,
       ROUND(m.mcap_cr::numeric, 0) AS mcap_cr,
       u.sector                   AS yahoo_sector,
       u.valvo_sector,
       u.industry,
       slq.latest_date            AS latest_period,
       (CURRENT_DATE - slq.latest_date) AS data_age_days,
       CASE WHEN hc.security_id IS NULL AND ha.security_id IS NOT NULL THEN 1 ELSE 0 END AS is_geo_only,
       CASE WHEN ha.security_id IS NULL THEN 1 ELSE 0 END AS no_seg_data,
       COALESCE(mc.num_major, 0)  AS num_major_segs,

       LEFT(ls1.segment_name, 120) AS seg1_name,
       ROUND(ls1.segment_revenue_cr::numeric, 2)  AS seg1_rev_cr,
       ls1.segment_revenue_pct    AS seg1_pct,
       ROUND(100 * (st1.ttm_r - st1.ttm_p) / NULLIF(st1.ttm_p,0), 1) AS seg1_yoy_pct,
       (st1.qc_r || '/' || st1.qc_p) AS seg1_q_basis,

       LEFT(ls2.segment_name, 120) AS seg2_name,
       ROUND(ls2.segment_revenue_cr::numeric, 2)  AS seg2_rev_cr,
       ls2.segment_revenue_pct    AS seg2_pct,
       ROUND(100 * (st2.ttm_r - st2.ttm_p) / NULLIF(st2.ttm_p,0), 1) AS seg2_yoy_pct,
       (st2.qc_r || '/' || st2.qc_p) AS seg2_q_basis,

       LEFT(ls3.segment_name, 120) AS seg3_name,
       ROUND(ls3.segment_revenue_cr::numeric, 2)  AS seg3_rev_cr,
       ls3.segment_revenue_pct    AS seg3_pct,
       ROUND(100 * (st3.ttm_r - st3.ttm_p) / NULLIF(st3.ttm_p,0), 1) AS seg3_yoy_pct,
       (st3.qc_r || '/' || st3.qc_p) AS seg3_q_basis,

       LEFT(ls4.segment_name, 120) AS seg4_name,
       ROUND(ls4.segment_revenue_cr::numeric, 2)  AS seg4_rev_cr,
       ls4.segment_revenue_pct    AS seg4_pct,
       ROUND(100 * (st4.ttm_r - st4.ttm_p) / NULLIF(st4.ttm_p,0), 1) AS seg4_yoy_pct,
       (st4.qc_r || '/' || st4.qc_p) AS seg4_q_basis

  FROM stock_universe u
  LEFT JOIN mcap_j            m   ON m.security_id   = u.security_id
  LEFT JOIN stock_last_qtr    slq ON slq.security_id = u.security_id
  LEFT JOIN has_any           ha  ON ha.security_id  = u.security_id
  LEFT JOIN has_clean         hc  ON hc.security_id  = u.security_id
  LEFT JOIN major_count       mc  ON mc.security_id  = u.security_id
  LEFT JOIN latest_segs ls1 ON ls1.security_id = u.security_id AND ls1.rk = 1
  LEFT JOIN seg_ttm     st1 ON st1.security_id = u.security_id AND st1.seg_key = ls1.seg_key
  LEFT JOIN latest_segs ls2 ON ls2.security_id = u.security_id AND ls2.rk = 2 AND ls2.segment_revenue_pct >= 20
  LEFT JOIN seg_ttm     st2 ON st2.security_id = u.security_id AND st2.seg_key = ls2.seg_key
  LEFT JOIN latest_segs ls3 ON ls3.security_id = u.security_id AND ls3.rk = 3 AND ls3.segment_revenue_pct >= 20
  LEFT JOIN seg_ttm     st3 ON st3.security_id = u.security_id AND st3.seg_key = ls3.seg_key
  LEFT JOIN latest_segs ls4 ON ls4.security_id = u.security_id AND ls4.rk = 4 AND ls4.segment_revenue_pct >= 20
  LEFT JOIN seg_ttm     st4 ON st4.security_id = u.security_id AND st4.seg_key = ls4.seg_key
 WHERE COALESCE(u.is_active, true) = true
 ORDER BY m.mcap_cr DESC NULLS LAST, u.symbol;
"""


def main():
    out_path = REPO / "docs" / "stock_segments_master.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD"),
        port=int(os.getenv("DB_PORT", 6543)),
        sslmode="require",
        cursor_factory=RealDictCursor,
        connect_timeout=20,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No rows returned.")
        return

    header = list(rows[0].keys())
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        for r in rows:
            w.writerow([r[k] for k in header])

    print(f"wrote {len(rows)} rows → {out_path}")

    # Quick coverage summary
    total = len(rows)
    with_seg1 = sum(1 for r in rows if r["seg1_name"])
    with_yoy  = sum(1 for r in rows if r["seg1_yoy_pct"] is not None)
    geo_only  = sum(1 for r in rows if r["is_geo_only"] == 1)
    no_seg    = sum(1 for r in rows if r["no_seg_data"] == 1)
    multi_seg = sum(1 for r in rows if r["num_major_segs"] and r["num_major_segs"] >= 2)
    with_mcap = sum(1 for r in rows if r["mcap_cr"])
    stale180  = sum(1 for r in rows if r["data_age_days"] and r["data_age_days"] > 180)

    print(
        f"\ncoverage:\n"
        f"  total active           : {total}\n"
        f"  with usable seg1       : {with_seg1} ({100*with_seg1/total:.1f}%)\n"
        f"  with seg1 YoY          : {with_yoy} ({100*with_yoy/total:.1f}%)\n"
        f"  has ≥2 major (≥20%) seg: {multi_seg}\n"
        f"  geographic-only        : {geo_only}\n"
        f"  no seg data at all     : {no_seg}\n"
        f"  stale (>180d old)      : {stale180}\n"
        f"  with market cap        : {with_mcap} ({100*with_mcap/total:.1f}%)\n"
    )


if __name__ == "__main__":
    main()
