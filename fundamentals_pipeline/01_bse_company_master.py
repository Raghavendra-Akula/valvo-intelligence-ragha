"""
=============================================================
VALVO INTELLIGENCE — FUNDAMENTALS PIPELINE
Script 01: BSE Company Master
=============================================================

WHAT THIS SCRIPT DOES:
    Fetches the master list of all equity stocks from BSE.
    Stores identity data (BSE code, ISIN, sector, industry,
    face value, listing date) in the bse_company_master table.
    Also links each company to stock_universe via symbol match.

WHEN IT RUNS:
    Every Sunday night (weekly refresh is enough)

HOW TO RUN MANUALLY:
    python3 01_bse_company_master.py

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
# PATHS — everything relative to this script's location
# Works regardless of username (Vamshi, rohit, etc.)
# ------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent          # ~/fundamentals/
LOG_DIR   = BASE_DIR.parent / "logs"       # ~/logs/
LOG_DIR.mkdir(parents=True, exist_ok=True) # Create if doesn't exist

# ------------------------------------------------------------------
# LOAD ENVIRONMENT VARIABLES from .env file
# ------------------------------------------------------------------
load_dotenv(BASE_DIR / ".env")

# ------------------------------------------------------------------
# LOGGING SETUP
# Writes to ~/logs/bse_company_master.log AND shows on screen
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'bse_company_master.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

BSE_EQUITY_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}


# ------------------------------------------------------------------
# STEP 1: CONNECT TO SUPABASE
# ------------------------------------------------------------------
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env file")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------------------------------------------
# STEP 2: FETCH DATA FROM BSE
# ------------------------------------------------------------------
def fetch_bse_equity_list() -> list:
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"Fetching BSE equity list (attempt {attempt}/{MAX_RETRIES})...")
            response = requests.get(BSE_EQUITY_LIST_URL, headers=HEADERS, timeout=30)
            response.raise_for_status()
            data = response.json()
            companies = data if isinstance(data, list) else data.get('Table', [])
            log.info(f"✓ Fetched {len(companies)} companies from BSE")
            return companies

        except requests.exceptions.Timeout:
            log.warning(f"Request timed out (attempt {attempt})")
            if attempt < MAX_RETRIES:
                time.sleep(5)

        except requests.exceptions.HTTPError as e:
            log.error(f"HTTP error: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(10)

        except Exception as e:
            log.error(f"Unexpected error: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5)

    log.error("All retries failed. Could not fetch BSE equity list.")
    return []


# ------------------------------------------------------------------
# STEP 3: CLEAN AND PARSE ONE COMPANY
# ------------------------------------------------------------------
def parse_company(raw: dict):
    try:
        bse_code = str(raw.get('SCRIP_CD', '')).strip()
        if not bse_code:
            return None

        company_name = str(raw.get('Issuer_Name', '')).strip()
        if not company_name:
            return None

        # scrip_id = NSE trading symbol (e.g. 'ABB', 'RELIANCE')
        symbol       = str(raw.get('scrip_id', '')).strip().upper()
        # ISIN — treat "NA", empty strings as None
        isin_raw     = str(raw.get('ISIN_NUMBER', '')).strip()
        isin         = None if isin_raw in ('NA', 'N/A', '', 'null', 'NULL', 'None') else isin_raw
        face_value   = safe_float(raw.get('FACE_VALUE'))
        # Sector not available in this BSE endpoint — fetched separately later
        sector       = None
        # Industry is present but often null in BSE response
        industry_raw = raw.get('INDUSTRY')
        industry     = str(industry_raw).strip().title() if industry_raw else None
        # Listing date not in this endpoint — fetched from BSE company detail later
        listing_date = None
        # GROUP = BSE trading group (A, B, T, etc.)
        group_name   = str(raw.get('GROUP', '')).strip()
        # Store the BSE URL for reference
        bse_url      = str(raw.get('NSURL', '')).strip()
        # Short trading name (e.g. "ABB India Ltd" vs full "ABB India Limited")
        short_name   = str(raw.get('Scrip_Name', '')).strip()
        # Market cap in crores — useful for quick filtering
        market_cap_cr = safe_float(raw.get('Mktcap'))
        # Segment — Equity, SME, Debt etc.
        segment      = str(raw.get('Segment', '')).strip()

        return {
            'bse_code':        bse_code,
            'isin':            isin if isin else None,
            'company_name':    company_name,
            'symbol':          symbol if symbol else None,
            'sector':          sector if sector else None,
            'industry':        industry if industry else None,
            'face_value':      face_value,
            'listing_date':    listing_date,
            'status':          'ACTIVE',
            'group_name':      group_name if group_name else None,
            'website':         bse_url if bse_url else None,
            'short_name':      short_name if short_name else None,
            'market_cap_cr':   market_cap_cr,
            'segment':         segment if segment else None,
            'instrument_type': 'EQUITY',
            'updated_at':      datetime.utcnow().isoformat()
        }

    except Exception as e:
        log.warning(f"Error parsing company {raw.get('SCRIP_CD', 'UNKNOWN')}: {e}")
        return None


# ------------------------------------------------------------------
# HELPER: safe number conversion
# ------------------------------------------------------------------
def safe_float(value):
    if value is None:
        return None
    try:
        return float(str(value).strip().replace(',', ''))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# HELPER: parse BSE date formats
# ------------------------------------------------------------------
def parse_date(date_str: str):
    if not date_str or str(date_str).strip() == '':
        return None
    date_str = str(date_str).strip()
    formats = ["%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    log.debug(f"Could not parse date: {date_str}")
    return None


# ------------------------------------------------------------------
# STEP 4: LOAD STOCK UNIVERSE FOR LINKING
# ------------------------------------------------------------------
def get_stock_universe_symbols(supabase: Client) -> dict:
    log.info("Loading stock_universe symbols for matching...")
    # Paginate through all records — Supabase default limit is 1000
    symbol_map = {}
    page = 0
    PAGE_SIZE = 1000

    while True:
        result = supabase.table('stock_universe')            .select('security_id, symbol')            .range(page * PAGE_SIZE, (page + 1) * PAGE_SIZE - 1)            .execute()

        for row in result.data:
            symbol = row.get('symbol', '').strip().upper()
            if symbol:
                symbol_map[symbol] = row['security_id']

        if len(result.data) < PAGE_SIZE:
            break
        page += 1

    log.info(f"Loaded {len(symbol_map)} symbols from stock_universe")
    return symbol_map


# ------------------------------------------------------------------
# STEP 5: SAVE TO SUPABASE IN BATCHES
# ------------------------------------------------------------------
def save_to_supabase(supabase: Client, companies: list):
    BATCH_SIZE = 100
    success_count = 0
    failure_count = 0

    for i in range(0, len(companies), BATCH_SIZE):
        batch = companies[i: i + BATCH_SIZE]
        try:
            supabase.table('bse_company_master').upsert(
                batch, on_conflict='bse_code'
            ).execute()
            success_count += len(batch)
            log.info(f"Saved batch {i // BATCH_SIZE + 1}: "
                     f"{len(batch)} companies (total: {success_count})")
            time.sleep(0.1)
        except Exception as e:
            failure_count += len(batch)
            log.error(f"Error saving batch at index {i}: {e}")

    return success_count, failure_count


# ------------------------------------------------------------------
# STEP 6: LOG RUN TO SUPABASE
# ------------------------------------------------------------------
def log_run_to_db(supabase: Client, started_at: datetime,
                  attempted: int, succeeded: int, failed: int,
                  status: str, error_msg: str = None):
    completed_at = datetime.utcnow()
    duration = (completed_at - started_at).total_seconds() / 60
    try:
        supabase.table('pipeline_logs').insert({
            'run_date':            date.today().isoformat(),
            'run_type':            'BSE_COMPANY_MASTER',
            'companies_attempted': attempted,
            'companies_succeeded': succeeded,
            'companies_failed':    failed,
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
    log.info("BSE Company Master Pipeline — Starting")
    log.info(f"Started at: {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log.info("=" * 60)

    # Connect
    try:
        supabase = get_supabase_client()
        log.info("✓ Connected to Supabase")
    except Exception as e:
        log.error(f"Cannot connect to Supabase: {e}")
        return

    # Fetch
    raw_companies = fetch_bse_equity_list()
    if not raw_companies:
        log.error("No data fetched from BSE. Exiting.")
        log_run_to_db(supabase, started_at, 0, 0, 0, 'FAILED', 'BSE fetch empty')
        return

    # Load universe for linking
    symbol_map = get_stock_universe_symbols(supabase)

    # Parse
    log.info(f"Parsing {len(raw_companies)} companies...")
    parsed_companies = []
    skipped = 0

    for raw in raw_companies:
        parsed = parse_company(raw)
        if parsed is None:
            skipped += 1
            continue
        symbol = parsed.get('symbol', '')
        parsed['security_id'] = symbol_map.get(symbol)
        parsed_companies.append(parsed)

    log.info(f"✓ Parsed: {len(parsed_companies)} valid | Skipped: {skipped}")

    # Save
    log.info("Saving to bse_company_master...")
    success_count, failure_count = save_to_supabase(supabase, parsed_companies)

    # Log run
    status = 'COMPLETED' if failure_count == 0 else 'PARTIAL'
    log_run_to_db(supabase, started_at,
                  attempted=len(parsed_companies),
                  succeeded=success_count,
                  failed=failure_count,
                  status=status)

    # Summary
    duration = (datetime.utcnow() - started_at).total_seconds()
    log.info("=" * 60)
    log.info("BSE Company Master Pipeline — Complete")
    log.info(f"Fetched:   {len(raw_companies)}")
    log.info(f"Parsed:    {len(parsed_companies)}")
    log.info(f"Saved:     {success_count}")
    log.info(f"Failed:    {failure_count}")
    log.info(f"Skipped:   {skipped}")
    log.info(f"Duration:  {duration:.1f} seconds")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
