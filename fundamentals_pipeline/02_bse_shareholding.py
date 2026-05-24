"""
=============================================================
VALVO INTELLIGENCE — FUNDAMENTALS PIPELINE
Script 02: Shareholding Pattern (via BSE)
=============================================================

WHAT THIS SCRIPT DOES:
    Fetches quarterly shareholding patterns for all stocks
    using BSE API — which is reliable and not behind Cloudflare.
    Stores Promoter%, FII%, DII%, Public% in
    the shareholding_quarterly table.

WHY BSE INSTEAD OF NSE:
    NSE APIs are behind Cloudflare protection (2023 onwards)
    which blocks automated scripts. BSE provides the exact same
    shareholding data without these restrictions.
    Both NSE and BSE receive the same filings from companies.

WHEN IT RUNS:
    Quarterly — after filing deadline
    (usually 3 weeks after each quarter end)

HOW TO RUN MANUALLY:
    python3 02_bse_shareholding.py

=============================================================
"""

import requests
import time
import logging
import os
from pathlib import Path
from datetime import datetime, date
from dotenv import load_dotenv
from supabase import create_client, Client

# ------------------------------------------------------------------
# PATHS AND ENVIRONMENT
# ------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# ------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'bse_shareholding.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

# BSE shareholding API — same headers as Script 01, no Cloudflare
BSE_SHAREHOLDING_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ShareHoldingPatterns/w"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.bseindia.com/",
    "Origin":          "https://www.bseindia.com",
}

# Delay between requests — be polite to BSE
DELAY_BETWEEN_REQUESTS = 0.3

# Batch size for saving to Supabase
BATCH_SIZE = 50


# ------------------------------------------------------------------
# STEP 1: CONNECT TO SUPABASE
# ------------------------------------------------------------------
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------------------------------------------
# STEP 2: LOAD ALL BSE STOCKS WITH THEIR BSE CODES
# We get stocks from bse_company_master (Script 01 output)
# filtered to only those linked to stock_universe (our NSE stocks)
# ------------------------------------------------------------------
def get_stocks_to_process(supabase: Client) -> list:
    """
    Returns list of {bse_code, symbol, security_id} for processing.

    We join bse_company_master with stock_universe to get only
    the stocks VALVO tracks — not all 5000 BSE companies.

    For stocks not in stock_universe, we still process them
    if they have a BSE code — fundamentals are useful for all.
    """
    log.info("Loading stocks to process from bse_company_master...")

    result = supabase.table('bse_company_master')\
        .select('bse_code, symbol, security_id')\
        .eq('status', 'ACTIVE')\
        .eq('segment', 'Equity')\
        .not_.is_('bse_code', 'null')\
        .execute()

    stocks = [
        {
            'bse_code':    r['bse_code'],
            'symbol':      r['symbol'],
            'security_id': r['security_id']
        }
        for r in result.data
        if r.get('bse_code')
    ]

    log.info(f"Loaded {len(stocks)} stocks to process")
    return stocks


# ------------------------------------------------------------------
# STEP 3: FETCH SHAREHOLDING FOR ONE STOCK FROM BSE
# ------------------------------------------------------------------
def fetch_shareholding(bse_code: str) -> list:
    """
    Fetches shareholding pattern history for one BSE code.

    BSE API returns data for multiple quarters at once.
    Typically last 8 quarters of history.

    Example URL:
    https://api.bseindia.com/BseIndiaAPI/api/ShareHoldingPatterns/w
    ?scripcode=500325

    Example response:
    {
      "ShareHoldingPatterns": [
        {
          "Quarter": "December 2024",
          "Promoters": "50.32",
          "ForeignInstitutions": "23.14",
          "DIIs": "14.21",
          "NonInstPublic": "12.33",
          "TotalShares": "6763067172"
        },
        ...
      ]
    }
    """
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                BSE_SHAREHOLDING_URL,
                params={'scripcode': bse_code},
                headers=HEADERS,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()

            # BSE wraps data in ShareHoldingPatterns key
            records = (
                data.get('ShareHoldingPatterns') or
                data.get('shareholdingPatterns') or
                data.get('Table') or
                (data if isinstance(data, list) else [])
            )

            return records if records else []

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                time.sleep(3)

        except requests.exceptions.HTTPError as e:
            log.debug(f"HTTP error for {bse_code}: {e}")
            return []

        except Exception as e:
            log.debug(f"Error fetching {bse_code}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2)

    return []


# ------------------------------------------------------------------
# STEP 4: PARSE BSE SHAREHOLDING RESPONSE
# ------------------------------------------------------------------
def parse_shareholding(raw_records: list, symbol: str,
                        security_id: str, bse_code: str) -> list:
    """
    Parses BSE shareholding response into clean records.

    BSE is cleaner than NSE — it already sends one row per quarter
    with all categories combined. No pivot needed.

    We just map field names and convert types.
    """
    parsed = []

    for record in raw_records:
        try:
            # Get quarter string and convert to date
            quarter_str  = (
                record.get('Quarter') or
                record.get('quarter') or
                record.get('QUARTER', '')
            )
            period_date = parse_quarter_to_date(quarter_str)
            if not period_date:
                continue

            # Map BSE field names to our columns
            # BSE may use different key names — check all variants
            promoter = safe_float(
                record.get('Promoters') or
                record.get('promoterAndPromoterGroup') or
                record.get('PROMOTERS')
            )
            promoter_pledge = safe_float(
                record.get('PromoterPledge') or
                record.get('promotersPledged') or
                record.get('PROMOTER_PLEDGE')
            )
            fii = safe_float(
                record.get('ForeignInstitutions') or
                record.get('foreignPortfolioInvestors') or
                record.get('FII') or
                record.get('ForeignInstitution')
            )
            dii = safe_float(
                record.get('DIIs') or
                record.get('dii') or
                record.get('DII') or
                record.get('domesticInstitutions')
            )
            mutual_funds = safe_float(
                record.get('MutualFunds') or
                record.get('mutualFunds') or
                record.get('MUTUAL_FUNDS')
            )
            insurance = safe_float(
                record.get('Insurance') or
                record.get('insuranceCompanies') or
                record.get('INSURANCE')
            )
            govt = safe_float(
                record.get('Government') or
                record.get('centralGovt') or
                record.get('GOVERNMENT')
            )
            public = safe_float(
                record.get('NonInstPublic') or
                record.get('publicShareholding') or
                record.get('PUBLIC') or
                record.get('nonInstitutionalPublic')
            )
            total_shares = safe_int(
                record.get('TotalShares') or
                record.get('totalShares') or
                record.get('TOTAL_SHARES')
            )

            parsed.append({
                'security_id':           security_id,
                'symbol':                symbol,
                'period':                quarter_str,
                'period_end_date':       period_date,
                'promoter_percent':      promoter,
                'promoter_pledge_percent': promoter_pledge,
                'fii_percent':           fii,
                'dii_percent':           dii,
                'mutual_fund_percent':   mutual_funds,
                'insurance_percent':     insurance,
                'government_percent':    govt,
                'public_percent':        public,
                'total_shares':          total_shares,
                'source_url': (
                    f"{BSE_SHAREHOLDING_URL}?scripcode={bse_code}"
                ),
            })

        except Exception as e:
            log.debug(f"Error parsing record for {symbol}: {e}")
            continue

    return parsed


# ------------------------------------------------------------------
# HELPER: Parse "December 2024" → "2024-12-31"
# ------------------------------------------------------------------
def parse_quarter_to_date(quarter_str: str) -> str | None:
    """
    Converts quarter string to period end date.

    Indian financial quarters:
    March     → Q4 ends 31 March
    June      → Q1 ends 30 June
    September → Q2 ends 30 September
    December  → Q3 ends 31 December
    """
    MONTH_END = {
        'march':     '03-31', 'mar': '03-31',
        'june':      '06-30', 'jun': '06-30',
        'september': '09-30', 'sep': '09-30',
        'december':  '12-31', 'dec': '12-31',
    }

    try:
        parts = str(quarter_str).strip().lower().split()
        if len(parts) < 2:
            return None
        month    = parts[0]
        year     = parts[-1]   # Take last part as year (handles "Q3 December 2024")
        end_date = MONTH_END.get(month)
        if not end_date or not year.isdigit():
            return None
        return f"{year}-{end_date}"
    except Exception:
        return None


# ------------------------------------------------------------------
# HELPERS: Type conversions
# ------------------------------------------------------------------
def safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip().replace(',', ''))
    except (ValueError, TypeError):
        return None


def safe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip().replace(',', '').split('.')[0])
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# STEP 5: SAVE TO SUPABASE IN BATCHES
# ------------------------------------------------------------------
def save_records(supabase: Client, records: list) -> tuple[int, int]:
    """Saves shareholding records in batches."""
    if not records:
        return 0, 0

    success = 0
    failed  = 0

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i: i + BATCH_SIZE]
        try:
            supabase.table('shareholding_quarterly').upsert(
                batch,
                on_conflict='security_id,period_end_date'
            ).execute()
            success += len(batch)
        except Exception as e:
            failed += len(batch)
            log.error(f"Error saving batch: {e}")

    return success, failed


# ------------------------------------------------------------------
# STEP 6: LOG RUN TO SUPABASE
# ------------------------------------------------------------------
def log_run_to_db(supabase: Client, started_at: datetime,
                  attempted: int, succeeded: int, failed: int,
                  records_saved: int, status: str,
                  error_msg: str = None):
    completed_at = datetime.utcnow()
    duration     = (completed_at - started_at).total_seconds() / 60
    try:
        supabase.table('pipeline_logs').insert({
            'run_date':            date.today().isoformat(),
            'run_type':            'BSE_SHAREHOLDING',
            'companies_attempted': attempted,
            'companies_succeeded': succeeded,
            'companies_failed':    failed,
            'shareholding_added':  records_saved,
            'started_at':          started_at.isoformat(),
            'completed_at':        completed_at.isoformat(),
            'duration_minutes':    round(duration, 2),
            'status':              status,
            'error_message':       error_msg,
        }).execute()
    except Exception as e:
        log.error(f"Could not write to pipeline_logs: {e}")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    started_at = datetime.utcnow()
    log.info("=" * 60)
    log.info("BSE Shareholding Pipeline — Starting")
    log.info(f"Started at: {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log.info("=" * 60)

    # Connect
    try:
        supabase = get_supabase_client()
        log.info("✓ Connected to Supabase")
    except Exception as e:
        log.error(f"Cannot connect to Supabase: {e}")
        return

    # Load stocks
    stocks = get_stocks_to_process(supabase)
    if not stocks:
        log.error("No stocks found to process")
        return

    # Process each stock
    total_attempted = 0
    total_succeeded = 0
    total_failed    = 0
    total_records   = 0
    all_records     = []

    for i, stock in enumerate(stocks):
        bse_code    = stock['bse_code']
        symbol      = stock['symbol'] or bse_code
        security_id = stock['security_id']

        total_attempted += 1

        # Fetch from BSE
        raw = fetch_shareholding(bse_code)

        if not raw:
            total_failed += 1
            log.debug(f"No data for {symbol} ({bse_code})")
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Parse into clean records
        parsed = parse_shareholding(raw, symbol, security_id, bse_code)

        if not parsed:
            total_failed += 1
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        total_succeeded += 1
        all_records.extend(parsed)

        # Save in batches of 500 records
        if len(all_records) >= 500:
            ok, fail = save_records(supabase, all_records)
            total_records += ok
            all_records    = []
            log.info(f"Progress: {i+1}/{len(stocks)} stocks | "
                     f"{total_records} records saved")

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Save remaining records
    if all_records:
        ok, fail = save_records(supabase, all_records)
        total_records += ok

    # Log run
    status = 'COMPLETED' if total_failed < total_attempted * 0.1 else 'PARTIAL'
    log_run_to_db(
        supabase, started_at,
        attempted=total_attempted,
        succeeded=total_succeeded,
        failed=total_failed,
        records_saved=total_records,
        status=status
    )

    # Summary
    duration = (datetime.utcnow() - started_at).total_seconds() / 60
    log.info("=" * 60)
    log.info("BSE Shareholding Pipeline — Complete")
    log.info(f"Stocks attempted: {total_attempted}")
    log.info(f"Stocks succeeded: {total_succeeded}")
    log.info(f"Stocks failed:    {total_failed}")
    log.info(f"Records saved:    {total_records}")
    log.info(f"Duration:         {duration:.1f} minutes")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
