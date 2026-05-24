"""Fix standalone annual data — synthesize from quarterly sums."""
import os, psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

conn = psycopg2.connect(
    host=os.getenv('DB_HOST'), database='postgres',
    user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'),
    port=6543, sslmode='require', cursor_factory=RealDictCursor
)
cur = conn.cursor()

# Synthesize annual from standalone quarterly
cur.execute("""
INSERT INTO financials_annual (
    security_id, symbol, fiscal_year, period_end_date,
    revenue_cr, expenses_cr, operating_profit_cr, opm_percent,
    other_income_cr, depreciation_cr, interest_cr,
    profit_before_tax_cr, tax_cr, net_profit_cr, eps,
    is_consolidated, is_audited
)
SELECT
    q.security_id, q.symbol,
    CASE
      WHEN q.fy_end = '2025-03-31' THEN 'FY2024-25'
      WHEN q.fy_end = '2024-03-31' THEN 'FY2023-24'
      WHEN q.fy_end = '2023-03-31' THEN 'FY2022-23'
      WHEN q.fy_end = '2022-03-31' THEN 'FY2021-22'
      WHEN q.fy_end = '2021-03-31' THEN 'FY2020-21'
    END,
    q.fy_end,
    q.revenue_cr, q.expenses_cr, q.operating_profit_cr,
    CASE WHEN q.revenue_cr > 0 THEN ROUND((q.operating_profit_cr / q.revenue_cr * 100)::numeric, 2) END,
    q.other_income_cr, q.depreciation_cr, q.interest_cr,
    q.profit_before_tax_cr, q.tax_cr, q.net_profit_cr, q.eps,
    false, false
FROM (
    SELECT
        fq.security_id, fq.symbol,
        CASE
            WHEN fq.period_end_date BETWEEN '2020-04-01' AND '2021-03-31' THEN '2021-03-31'::date
            WHEN fq.period_end_date BETWEEN '2021-04-01' AND '2022-03-31' THEN '2022-03-31'::date
            WHEN fq.period_end_date BETWEEN '2022-04-01' AND '2023-03-31' THEN '2023-03-31'::date
            WHEN fq.period_end_date BETWEEN '2023-04-01' AND '2024-03-31' THEN '2024-03-31'::date
            WHEN fq.period_end_date BETWEEN '2024-04-01' AND '2025-03-31' THEN '2025-03-31'::date
        END as fy_end,
        SUM(fq.revenue_cr) as revenue_cr,
        SUM(fq.expenses_cr) as expenses_cr,
        SUM(fq.operating_profit_cr) as operating_profit_cr,
        SUM(fq.other_income_cr) as other_income_cr,
        SUM(fq.depreciation_cr) as depreciation_cr,
        SUM(fq.interest_cr) as interest_cr,
        SUM(fq.profit_before_tax_cr) as profit_before_tax_cr,
        SUM(fq.tax_cr) as tax_cr,
        SUM(fq.net_profit_cr) as net_profit_cr,
        SUM(fq.eps) as eps,
        COUNT(*) as q_count
    FROM financials_quarterly fq
    WHERE fq.is_consolidated = false AND fq.revenue_cr IS NOT NULL
    GROUP BY fq.security_id, fq.symbol, fy_end
    HAVING COUNT(*) >= 3
) q
WHERE q.fy_end IS NOT NULL
ON CONFLICT ON CONSTRAINT uq_financials_annual_symbol_period DO NOTHING
""")
print(f"Synthesized {cur.rowcount} standalone annual rows")
conn.commit()

cur.close()
conn.close()
