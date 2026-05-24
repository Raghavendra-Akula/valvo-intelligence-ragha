"""
=============================================================
VALVO INTELLIGENCE — FUNDAMENTALS PIPELINE
Script 02: NSE Shareholding Pattern
=============================================================

WHAT THIS SCRIPT DOES:
    Fetches quarterly shareholding patterns for all NSE stocks.
    Stores Promoter%, FII%, DII%, Public% history in
    the shareholding_quarterly table.

KEY DIFFERENCE FROM SCRIPT 01:
    NSE requires an active browser session (cookies).
    We first visit NSE homepage to get session cookies,
    then use those cookies for all API calls.
    Without this step, NSE returns empty/blocked responses.

WHEN IT RUNS:
    Quarterly — after shareholding filing deadline
    (usually 3 weeks after quarter end)

HOW TO RUN MANUALLY:
    python3 02_nse_shareholding.py

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
        logging.FileHandler(LOG_DIR / 'nse_shareholding.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

# NSE endpoints
NSE_HOME_URL         = "https://www.nseindia.com"
NSE_SHAREHOLDING_URL = "https://www.nseindia.com/api/corporate-shareholding-patterns"

# Browser headers — NSE is strict, needs complete set
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Referer":          "https://www.nseindia.com/",
    "Origin":           "https://www.nseindia.com",
    "Connection":       "keep-alive",
}

# How many seconds to wait between each stock request
# NSE rate limits aggressive scrapers — 0.5s is safe
DELAY_BETWEEN_REQUESTS = 0.5

# Refresh NSE session every N stocks
# Session cookies expire after some time
SESSION_REFRESH_EVERY = 50


# ------------------------------------------------------------------
# STEP 1: CONNECT TO SUPABASE
# ------------------------------------------------------------------
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env file")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------------------------------------------
# STEP 2: CREATE NSE SESSION
# This is the key difference from Script 01.
# We use requests.Session() which automatically stores and
# sends cookies with every request — just like a browser does.
# ------------------------------------------------------------------
def create_nse_session() -> requests.Session:
    """
    Creates an authenticated NSE session by:
    1. Creating a Session object (stores cookies automatically)
    2. Visiting NSE homepage to get session cookies
    3. Returning the session for reuse

    WHY SESSION OBJECT:
    Without Session, each request is independent — no cookies.
    With Session, cookies from homepage are automatically sent
    with every subsequent request — NSE thinks we're a browser.
    """
    log.info("Creating NSE session (visiting homepage for cookies)...")

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        # Visit homepage — this sets session cookies
        response = session.get(NSE_HOME_URL, timeout=15)
        response.raise_for_status()

        log.info(f"✓ NSE session created (cookies: {len(session.cookies)} received)")
        return session

    except Exception as e:
        log.error(f"Failed to create NSE session: {e}")
        raise


# ------------------------------------------------------------------
# STEP 3: FETCH SHAREHOLDING FOR ONE STOCK
# ------------------------------------------------------------------
def fetch_shareholding(session: requests.Session, symbol: str) -> list:
    """
    Fetches shareholding pattern history for one stock symbol.

    NSE returns data for multiple quarters at once — typically
    the last 4-8 quarters of history.

    Returns list of quarter records, or empty list if failed.
    """
    try:
        params = {
            'index':  'equities',
            'symbol': symbol.upper()
        }

        response = session.get(
            NSE_SHAREHOLDING_URL,
            params=params,
            timeout=15
        )

        # NSE returns 401 or empty when session expires
        if response.status_code == 401:
            log.warning(f"Session expired for {symbol} — needs refresh")
            return None  # Signal to refresh session

        response.raise_for_status()

        data = response.json()

        # NSE wraps data inside 'data' key
        records = data.get('data', [])
        return records

    except requests.exceptions.Timeout:
        log.warning(f"Timeout fetching {symbol}")
        return []

    except Exception as e:
        log.warning(f"Error fetching {symbol}: {e}")
        return []


# ------------------------------------------------------------------
# STEP 4: PARSE SHAREHOLDING RECORDS
# NSE returns one record per shareholder category per quarter.
# We need to combine all categories into one row per quarter.
# ------------------------------------------------------------------
def parse_shareholding_records(raw_records: list, symbol: str,
                                security_id: str) -> list:
    """
    NSE sends shareholding data like this (multiple rows per quarter):

    quarter       | shareHolderType                  | percentOfShares
    Dec 2024      | Promoter & Promoter Group         | 50.32
    Dec 2024      | Foreign Portfolio Investors        | 23.14
    Dec 2024      | Mutual Funds                       | 10.21
    Sep 2024      | Promoter & Promoter Group         | 50.31
    Sep 2024      | Foreign Portfolio Investors        | 22.98
    ...

    We PIVOT this into one row per quarter:
    quarter   | promoter | fii   | dii   | public
    Dec 2024  | 50.32    | 23.14 | 14.21 | 12.33
    Sep 2024  | 50.31    | 22.98 | 14.55 | 12.16
    """

    # Group records by quarter
    quarters = {}

    for record in raw_records:
        quarter_str = record.get('quarter', '')
        if not quarter_str:
            continue

        if quarter_str not in quarters:
            quarters[quarter_str] = {
                'period':       quarter_str,       # 'December 2024'
                'symbol':       symbol,
                'security_id':  security_id,
                'promoter_percent':          None,
                'promoter_pledge_percent':   None,
                'fii_percent':               None,
                'dii_percent':               None,
                'mutual_fund_percent':        None,
                'insurance_percent':          None,
                'government_percent':         None,
                'public_percent':             None,
                'other_percent':              None,
                'total_shares':              None,
                'filing_date':               None,
            }

        holder_type = str(record.get('shareHolderType', '')).lower()
        pct = safe_float(record.get('percentOfShares'))
        shares = safe_int(record.get('noOfShares'))

        # Map NSE holder type names to our standard columns
        # NSE uses various names — we normalize them
        if 'promoter' in holder_type and 'pledge' not in holder_type:
            quarters[quarter_str]['promoter_percent'] = pct
            if shares and not quarters[quarter_str]['total_shares']:
                quarters[quarter_str]['total_shares'] = shares

        elif 'pledge' in holder_type:
            quarters[quarter_str]['promoter_pledge_percent'] = pct

        elif 'foreign portfolio' in holder_type or 'fii' in holder_type or 'fpi' in holder_type:
            quarters[quarter_str]['fii_percent'] = pct

        elif 'mutual fund' in holder_type:
            quarters[quarter_str]['mutual_fund_percent'] = pct

        elif 'insurance' in holder_type:
            quarters[quarter_str]['insurance_percent'] = pct

        elif 'government' in holder_type or 'central govt' in holder_type:
            quarters[quarter_str]['government_percent'] = pct

        elif any(x in holder_type for x in ['public', 'retail', 'individual']):
            quarters[quarter_str]['public_percent'] = pct

        elif any(x in holder_type for x in ['domestic institution', 'dii']):
            quarters[quarter_str]['dii_percent'] = pct

    # Convert quarter strings to standard date format and build final records
    parsed = []
    for quarter_str, data in quarters.items():
        period_date = parse_quarter_to_date(quarter_str)
        if period_date:
            data['period_end_date'] = period_date
            data['source_url'] = f"{NSE_SHAREHOLDING_URL}?symbol={symbol}"
            parsed.append(data)

    return parsed


# ------------------------------------------------------------------
# HELPER: Parse "December 2024" → "2024-12-31"
# ------------------------------------------------------------------
def parse_quarter_to_date(quarter_str: str) -> str | None:
    """
    Converts NSE quarter string to period end date.

    "December 2024"  → "2024-12-31"
    "September 2024" → "2024-09-30"
    "June 2024"      → "2024-06-30"
    "March 2024"     → "2024-03-31"

    Indian financial quarters end on:
    Q1: June 30
    Q2: September 30
    Q3: December 31
    Q4: March 31
    """
    MONTH_TO_END_DATE = {
        'march':     '03-31',
        'june':      '06-30',
        'september': '09-30',
        'december':  '12-31',
        # Short forms NSE sometimes uses
        'mar':       '03-31',
        'jun':       '06-30',
        'sep':       '09-30',
        'dec':       '12-31',
    }

    try:
        parts = quarter_str.strip().lower().split()
        if len(parts) < 2:
            return None

        month = parts[0]
        year  = parts[1]

        end_date_suffix = MONTH_TO_END_DATE.get(month)
        if not end_date_suffix:
            return None

        return f"{year}-{end_date_suffix}"

    except Exception:
        return None


# ------------------------------------------------------------------
# HELPERS: Safe type conversions
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
        return int(str(value).strip().replace(',', ''))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# STEP 5: LOAD STOCKS TO PROCESS
# We get the list from stock_universe — these are all NSE stocks
# We join with bse_company_master to get security_id mapping
# ------------------------------------------------------------------
def get_nse_stocks(supabase: Client) -> list:
    """
    Returns list of {symbol, security_id} for all active NSE stocks.
    We use stock_universe as the source — it has all stocks VALVO tracks.
    """
    log.info("Loading NSE stock list from stock_universe...")

    result = supabase.table('stock_universe')\
        .select('security_id, symbol')\
        .eq('is_active', True)\
        .execute()

    stocks = [
        {'symbol': r['symbol'], 'security_id': r['security_id']}
        for r in result.data
        if r.get('symbol')
    ]

    log.info(f"Loaded {len(stocks)} active NSE stocks")
    return stocks


# ------------------------------------------------------------------
# STEP 6: SAVE TO SUPABASE
# ------------------------------------------------------------------
def save_shareholding(supabase: Client, records: list) -> tuple[int, int]:
    """
    Saves shareholding records to shareholding_quarterly.
    Uses UPSERT on (security_id, period_end_date).
    """
    if not records:
        return 0, 0

    success = 0
    failed  = 0

    for record in records:
        try:
            supabase.table('shareholding_quarterly').upsert(
                record,
                on_conflict='security_id,period_end_date'
            ).execute()
            success += 1
        except Exception as e:
            failed += 1
            log.debug(f"Error saving record: {e}")

    return success, failed


# ------------------------------------------------------------------
# STEP 7: LOG RUN TO SUPABASE
# ------------------------------------------------------------------
def log_run_to_db(supabase: Client, started_at: datetime,
                  attempted: int, succeeded: int, failed: int,
                  status: str, error_msg: str = None):
    completed_at = datetime.utcnow()
    duration     = (completed_at - started_at).total_seconds() / 60
    try:
        supabase.table('pipeline_logs').insert({
            'run_date':            date.today().isoformat(),
            'run_type':            'NSE_SHAREHOLDING',
            'companies_attempted': attempted,
            'companies_succeeded': succeeded,
            'companies_failed':    failed,
            'shareholding_added':  succeeded,
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
    log.info("NSE Shareholding Pipeline — Starting")
    log.info(f"Started at: {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log.info("=" * 60)

    # Connect to Supabase
    try:
        supabase = get_supabase_client()
        log.info("✓ Connected to Supabase")
    except Exception as e:
        log.error(f"Cannot connect to Supabase: {e}")
        return

    # Get stock list
    stocks = get_nse_stocks(supabase)
    if not stocks:
        log.error("No stocks found in stock_universe")
        return

    # Create NSE session
    try:
        session = create_nse_session()
    except Exception as e:
        log.error(f"Cannot create NSE session: {e}")
        log_run_to_db(supabase, started_at, 0, 0, 0, 'FAILED', str(e))
        return

    # Process each stock
    total_attempted  = 0
    total_succeeded  = 0
    total_failed     = 0
    total_records    = 0

    for i, stock in enumerate(stocks):
        symbol      = stock['symbol']
        security_id = stock['security_id']

        # Refresh NSE session periodically
        if i > 0 and i % SESSION_REFRESH_EVERY == 0:
            log.info(f"Refreshing NSE session at stock {i}...")
            try:
                session = create_nse_session()
            except Exception as e:
                log.error(f"Session refresh failed: {e}")

        total_attempted += 1

        # Fetch shareholding data
        raw_records = fetch_shareholding(session, symbol)

        # None means session expired — refresh and retry once
        if raw_records is None:
            log.info(f"Session expired at {symbol} — refreshing...")
            try:
                session = create_nse_session()
                raw_records = fetch_shareholding(session, symbol)
            except Exception:
                raw_records = []

        if not raw_records:
            total_failed += 1
            log.debug(f"No data for {symbol}")
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Parse into structured records (one per quarter)
        parsed = parse_shareholding_records(raw_records, symbol, security_id)

        if not parsed:
            total_failed += 1
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Save to Supabase
        ok, fail = save_shareholding(supabase, parsed)
        total_records  += ok
        total_succeeded += 1 if ok > 0 else 0
        total_failed    += 1 if fail > 0 and ok == 0 else 0

        # Progress log every 50 stocks
        if (i + 1) % 50 == 0:
            log.info(f"Progress: {i+1}/{len(stocks)} stocks | "
                     f"{total_records} records saved so far")

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Log final run
    status = 'COMPLETED' if total_failed == 0 else 'PARTIAL'
    log_run_to_db(supabase, started_at,
                  attempted=total_attempted,
                  succeeded=total_succeeded,
                  failed=total_failed,
                  status=status)

    # Summary
    duration = (datetime.utcnow() - started_at).total_seconds() / 60
    log.info("=" * 60)
    log.info("NSE Shareholding Pipeline — Complete")
    log.info(f"Stocks processed: {total_attempted}")
    log.info(f"Stocks succeeded: {total_succeeded}")
    log.info(f"Stocks failed:    {total_failed}")
    log.info(f"Records saved:    {total_records}")
    log.info(f"Duration:         {duration:.1f} minutes")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
