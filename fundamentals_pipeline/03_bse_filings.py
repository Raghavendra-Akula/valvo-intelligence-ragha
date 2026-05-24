"""
=============================================================
VALVO INTELLIGENCE — FUNDAMENTALS PIPELINE
Script 03: BSE Filing Discovery
=============================================================

WHAT THIS SCRIPT DOES:
    Fetches all financial result filings from BSE for ALL
    5,000+ BSE listed companies. Stores:
    1. PDF links → filings table (for clients to view)
    2. XBRL links → filings table (for Script 04 to parse)

HOW BSE FILING API WORKS:
    - Returns 50 filings per page (newest first)
    - Financial results scattered among all filing types
    - We filter: CATEGORYNAME="Result" AND SUBCATNAME="Financial Results"
    - Paginate backwards in time until no more records

PDF URL:  bseindia.com/xml-data/corpfiling/AttachLive/{ATTACHMENTNAME}
XBRL URL: bseindia.com/xml-data/corpfiling/AttachHis/{XML_NAME}.xml

WHEN IT RUNS:
    Nightly — companies file results throughout results season

HOW TO RUN MANUALLY:
    python3 03_bse_filings.py

=============================================================
"""

import re                          # FIX 1: moved to top (was inside function)
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
        logging.FileHandler(LOG_DIR / 'bse_filings.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

BSE_FILINGS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
)

BSE_PDF_BASE  = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_XBRL_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"

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

# How far back to fetch filings
FETCH_FROM_DATE = "20100101"                        # 2010 onwards
FETCH_TO_DATE   = datetime.now().strftime("%Y%m%d") # Today

# FIX 2: Renamed to avoid confusion with BSE_PAGE_SIZE
BSE_PAGE_SIZE  = 50    # BSE API returns 50 filings per page
DB_PAGE_SIZE   = 1000  # Supabase pagination chunk size

# Delay between requests — be polite to BSE
DELAY_BETWEEN_REQUESTS = 0.3
DELAY_BETWEEN_PAGES    = 0.1  # Shorter delay between pages of same company

# Max pages per company — safety limit
# 50 records/page × 100 pages = 5,000 filings per company max
MAX_PAGES_PER_COMPANY = 100

# Filing categories we care about — confirmed from real Reliance API test
FINANCIAL_CATEGORIES = {
    ('Result', 'Financial Results'),
    ('Board Meeting', 'Board Meeting'),
    ('AGM/EGM', 'AGM'),
    ('AGM/EGM', 'EGM'),
    ('AGM/EGM', 'Postal Ballot'),
    ('Company Update', 'Investor Presentation'),
    ('Company Update', 'Earnings Call Transcript'),
}

# Map BSE category → our filing_type
FILING_TYPE_MAP = {
    ('Result', 'Financial Results'):                 'QUARTERLY_RESULT',
    ('Board Meeting', 'Board Meeting'):              'BOARD_MEETING',
    ('AGM/EGM', 'AGM'):                              'AGM',
    ('AGM/EGM', 'EGM'):                             'EGM',
    ('AGM/EGM', 'Postal Ballot'):                   'AGM',
    ('Company Update', 'Investor Presentation'):     'INVESTOR_PRESENTATION',
    ('Company Update', 'Earnings Call Transcript'):  'CONCALL_TRANSCRIPT',
}

# Month to Indian FY quarter mapping
MONTH_TO_QUARTER = {
    'june':      'Q1', 'jun': 'Q1',
    'september': 'Q2', 'sep': 'Q2',
    'december':  'Q3', 'dec': 'Q3',
    'march':     'Q4', 'mar': 'Q4',
}


# ------------------------------------------------------------------
# CREATE BSE SESSION WITH COOKIES
# ------------------------------------------------------------------
def create_bse_session() -> requests.Session:
    """
    Creates a browser session by visiting BSE pages to get cookies.
    BSE requires session cookies for filing API calls on some companies.
    Without cookies, BSE returns HTML instead of JSON.

    Visits multiple BSE pages to ensure cookies are set properly.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # Visit multiple BSE pages to get all required cookies
    urls_to_visit = [
        "https://www.bseindia.com",
        "https://www.bseindia.com/corporates/ann.html",
    ]

    for url in urls_to_visit:
        try:
            session.get(url, timeout=15)
        except Exception as e:
            log.debug(f"Could not visit {url}: {e}")

    cookie_count = len(session.cookies)
    if cookie_count > 0:
        log.info(f"✓ BSE session created ({cookie_count} cookies)")
    else:
        log.warning("BSE session created but no cookies received — some companies may fail")

    return session


# ------------------------------------------------------------------
# CONNECT TO SUPABASE
# ------------------------------------------------------------------
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Supabase credentials missing from .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------------------------------------------
# LOAD ALL BSE STOCKS
# ------------------------------------------------------------------
def get_stocks(supabase: Client) -> list:
    """
    Returns ALL active equity stocks from bse_company_master.
    Paginates in chunks of DB_PAGE_SIZE (1000) to get all records.
    Excludes Group X, Z, XT (suspended/delisted) — BSE doesn't serve their filings.

    security_id may be NULL for BSE-only companies not in
    stock_universe — filings table now allows this.
    """
    log.info("Loading ALL stocks from bse_company_master...")

    all_stocks = []
    page       = 0

    while True:
        result = supabase.table('bse_company_master')\
            .select('bse_code, symbol, security_id')\
            .eq('status', 'ACTIVE')\
            .eq('segment', 'Equity')\
            .not_.in_('group_name', ['X', 'Z', 'ZP'])\
            .range(page * DB_PAGE_SIZE, (page + 1) * DB_PAGE_SIZE - 1)\
            .execute()

        if not result.data:
            break

        batch = [
            {
                'bse_code':    r['bse_code'],
                'symbol':      r['symbol'] or r['bse_code'],
                'security_id': r['security_id'],
            }
            for r in result.data
            if r.get('bse_code')
        ]

        all_stocks.extend(batch)
        log.info(f"Loaded page {page+1}: {len(batch)} stocks "
                 f"(total: {len(all_stocks)})")

        if len(result.data) < DB_PAGE_SIZE:
            break
        page += 1

    log.info(f"✓ Total stocks to process: {len(all_stocks)}")
    return all_stocks


# ------------------------------------------------------------------
# FETCH FILINGS FOR ONE COMPANY
# ------------------------------------------------------------------
def fetch_filings_for_company(bse_code: str,
                               session: requests.Session) -> list | None:
    """
    Fetches all relevant filings for one BSE code using a shared session.
    Session carries BSE cookies — critical for getting JSON responses.

    Returns:
        list  → relevant filings found (may be empty if company has none)
        None  → actual fetch error (network/API failure)
    """
    relevant = []
    page     = 1

    while page <= MAX_PAGES_PER_COMPANY:
        MAX_PAGE_RETRIES = 3
        page_success     = False

        for retry in range(1, MAX_PAGE_RETRIES + 1):
            try:
                params = {
                    'strCat':      '-1',
                    'strPrevDate': FETCH_FROM_DATE,
                    'strScrip':    bse_code,
                    'strSearch':   'P',
                    'strToDate':   FETCH_TO_DATE,
                    'strType':     'C',
                    'pageno':      page,
                    'pagesize':    BSE_PAGE_SIZE,
                }

                response = session.get(
                    BSE_FILINGS_URL,
                    params=params,
                    timeout=15
                )
                response.raise_for_status()

                if not response.text.strip().startswith('{'):
                    # BSE returned HTML — likely throttling
                    # Retry with increasing delay
                    if retry < MAX_PAGE_RETRIES:
                        log.debug(f"HTML response for {bse_code} page {page} "
                                  f"(retry {retry}/{MAX_PAGE_RETRIES}) — waiting {retry*5}s")
                        time.sleep(retry * 5)  # 5s, 10s, 15s
                        continue
                    else:
                        # All retries exhausted
                        return None if page == 1 else relevant

                data    = response.json()
                records = data.get('Table', [])

                if not records:
                    return relevant  # No more pages — reached end

                # Filter for relevant filing types
                for rec in records:
                    cat    = rec.get('CATEGORYNAME', '').strip()
                    subcat = rec.get('SUBCATNAME', '').strip()
                    if (cat, subcat) in FINANCIAL_CATEGORIES:
                        relevant.append(rec)

                page_success = True
                break  # Page fetched successfully — move to next page

            except requests.exceptions.Timeout:
                if retry < MAX_PAGE_RETRIES:
                    log.debug(f"Timeout for {bse_code} page {page} "
                              f"(retry {retry}) — waiting {retry*3}s")
                    time.sleep(retry * 3)
                else:
                    return None if page == 1 else relevant

            except Exception as e:
                log.debug(f"Error for {bse_code} page {page}: {e}")
                return None if page == 1 else relevant

        if not page_success:
            break

        page += 1
        time.sleep(DELAY_BETWEEN_PAGES)

    return relevant


# ------------------------------------------------------------------
# PARSE ONE FILING RECORD
# ------------------------------------------------------------------
def parse_filing(record: dict, symbol: str,
                 security_id: str, bse_code: str) -> list:
    """
    Parses one BSE filing record. Returns a LIST because one
    BSE announcement generates two records:
    1. PDF record (for client display)
    2. XBRL record (for Script 04 to parse — financial results only)
    """
    results = []

    try:
        news_id     = record.get('NEWSID', '')
        cat         = record.get('CATEGORYNAME', '').strip()
        subcat      = record.get('SUBCATNAME', '').strip()
        headline    = record.get('HEADLINE', '').strip()

        # FIX 4: Strip HTML tags from NEWSSUB (BSE sends <BR> tags)
        newssub_raw  = record.get('NEWSSUB', '') or ''
        description  = re.sub(r'<[^>]+>', ' ', newssub_raw).strip() or headline

        filing_dt   = record.get('DT_TM', '')
        pdf_flag    = record.get('PDFFLAG', 0)
        attachment  = record.get('ATTACHMENTNAME', '').strip()
        xml_name    = record.get('XML_NAME', '').strip()

        # Parse filing date
        filing_date = parse_bse_datetime(filing_dt)
        if not filing_date:
            return []

        # Skip if no news_id — can't upsert without unique key
        # FIX 5: Don't save records with empty bse_filing_id
        if not news_id:
            return []

        # Determine filing type
        filing_type = FILING_TYPE_MAP.get((cat, subcat), 'OTHER')

        # Extract period from description
        period      = extract_period(description)
        fiscal_year = extract_fiscal_year(period)

        # Base record
        base = {
            'security_id':   security_id,
            'symbol':        symbol,
            'bse_code':      bse_code,
            'filing_type':   filing_type,
            'period':        period,
            'fiscal_year':   fiscal_year,
            'filing_date':   filing_date,
            'description':   description[:500] if description else None,
            'bse_filing_id': news_id,
            'exchange':      'BSE',
        }

        # PDF record
        if pdf_flag and attachment:
            pdf_record = dict(base)
            pdf_record['pdf_url'] = f"{BSE_PDF_BASE}{attachment}"
            results.append(pdf_record)

        # XBRL record — only for financial results, only if XML exists
        if xml_name and filing_type == 'QUARTERLY_RESULT':
            xbrl_record = dict(base)
            xbrl_record['pdf_url']       = f"{BSE_XBRL_BASE}{xml_name}.xml"
            xbrl_record['filing_type']   = 'XBRL_SOURCE'
            xbrl_record['bse_filing_id'] = f"{news_id}_XBRL"
            results.append(xbrl_record)

    except Exception as e:
        log.debug(f"Error parsing filing for {symbol}: {e}")

    return results


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def parse_bse_datetime(dt_str) -> str | None:
    """Converts BSE datetime "2024-10-14T18:58:35.22" → "2024-10-14"."""
    if not dt_str:
        return None
    try:
        return str(dt_str).strip()[:10]
    except Exception:
        return None


def extract_period(text: str) -> str | None:
    """
    Extracts Indian FY period from filing description text.

    "Quarter ended September 30, 2024"   → "Q2FY25"
    "Quarter ended December 31, 2024"    → "Q3FY25"
    "Quarter ended March 31, 2024"       → "Q4FY24"
    "Year ended March 31, 2024"          → "FY24"

    FIX 1: re is now imported at top — no import inside function
    """
    if not text:
        return None

    text_lower = text.lower()

    # Look for "Month DD, YYYY" or "Month YYYY" pattern
    match = re.search(
        r'(january|february|march|april|may|june|july|august|'
        r'september|october|november|december|jan|feb|mar|apr|'
        r'may|jun|jul|aug|sep|oct|nov|dec)\s+\d{0,2},?\s*(\d{4})',
        text_lower
    )

    if match:
        month   = match.group(1)
        year    = int(match.group(2))
        quarter = MONTH_TO_QUARTER.get(month)

        if quarter:
            # Convert calendar year to Indian FY
            # Apr-Dec of year X → FY(X+1)
            # Jan-Mar of year X → FY(X)
            if month in ('april', 'may', 'june', 'july', 'august',
                         'september', 'october', 'november', 'december',
                         'apr', 'jun', 'jul', 'aug', 'sep', 'oct',
                         'nov', 'dec'):
                fy = str(year + 1)[-2:]
            else:
                fy = str(year)[-2:]

            # Annual result check
            if 'year ended' in text_lower or 'annual' in text_lower:
                return f"FY{fy}"

            return f"{quarter}FY{fy}"

    return None


def extract_fiscal_year(period: str) -> str | None:
    """
    Extracts just the FY part from a period string.

    FIX 6: Takes period string directly, not description text.
    "Q2FY25" → "FY25"
    "FY24"   → "FY24"
    None     → None
    """
    if not period:
        return None
    if period.startswith('FY'):
        return period
    if 'FY' in period:
        return 'FY' + period.split('FY')[1]
    return None


# ------------------------------------------------------------------
# SAVE TO SUPABASE
# ------------------------------------------------------------------
def save_filings(supabase: Client, records: list) -> tuple[int, int]:
    """
    Saves filing records to filings table.
    Deduplicates within batch on bse_filing_id before saving.
    Upserts on bse_filing_id — safe to re-run multiple times.
    """
    if not records:
        return 0, 0

    # Deduplicate — keep last occurrence of each bse_filing_id
    # FIX 5: Skip records with empty bse_filing_id
    seen = {}
    for r in records:
        key = r.get('bse_filing_id', '').strip()
        if key:  # Only include records with a valid ID
            seen[key] = r
    deduped = list(seen.values())

    if not deduped:
        return 0, 0

    BATCH_SIZE = 100
    success    = 0
    failed     = 0

    for i in range(0, len(deduped), BATCH_SIZE):
        batch = deduped[i: i + BATCH_SIZE]
        try:
            supabase.table('filings').upsert(
                batch,
                on_conflict='bse_filing_id'
            ).execute()
            success += len(batch)
        except Exception as e:
            failed += len(batch)
            log.error(f"Error saving filings batch at {i}: {e}")

    return success, failed


# ------------------------------------------------------------------
# LOG RUN TO SUPABASE
# ------------------------------------------------------------------
def log_run_to_db(supabase: Client, started_at: datetime,
                  attempted: int, succeeded: int, failed: int,
                  no_data: int, records_saved: int, status: str,
                  error_msg: str = None):
    completed_at = datetime.utcnow()
    duration     = (completed_at - started_at).total_seconds() / 60
    try:
        supabase.table('pipeline_logs').insert({
            'run_date':            date.today().isoformat(),
            'run_type':            'BSE_FILINGS',
            'companies_attempted': attempted,
            'companies_succeeded': succeeded,
            'companies_failed':    failed,
            'new_filings_found':   records_saved,
            'started_at':          started_at.isoformat(),
            'completed_at':        completed_at.isoformat(),
            'duration_minutes':    round(duration, 2),
            'status':              status,
            'error_message':       error_msg,
            'notes': f"{no_data} companies had no relevant filings (valid)",
        }).execute()
    except Exception as e:
        log.error(f"Could not write to pipeline_logs: {e}")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    started_at = datetime.utcnow()
    log.info("=" * 60)
    log.info("BSE Filings Discovery Pipeline — Starting")
    log.info(f"Started at: {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log.info(f"Date range: {FETCH_FROM_DATE} → {FETCH_TO_DATE}")
    log.info("=" * 60)

    # Connect
    try:
        supabase = get_supabase_client()
        log.info("✓ Connected to Supabase")
    except Exception as e:
        log.error(f"Cannot connect to Supabase: {e}")
        return

    # Load stocks
    stocks = get_stocks(supabase)
    if not stocks:
        log.error("No stocks found in bse_company_master")
        return

    # Create BSE session with cookies
    bse_session = create_bse_session()

    # Process each stock
    total_attempted = 0
    total_succeeded = 0
    total_failed    = 0
    total_no_data   = 0   # Companies with no relevant filings (valid)
    total_records   = 0
    batch_buffer    = []

    for i, stock in enumerate(stocks):
        bse_code    = stock['bse_code']
        symbol      = stock['symbol']
        security_id = stock['security_id']

        total_attempted += 1

        # Refresh BSE session every 500 companies
        # Session cookies expire over time
        if i > 0 and i % 500 == 0:
            log.info(f"Refreshing BSE session at stock {i}...")
            bse_session = create_bse_session()

        # Fetch — None = actual error, [] = no filings (valid)
        raw_filings = fetch_filings_for_company(bse_code, bse_session)

        # FIX 3: Properly distinguish errors from empty data
        if raw_filings is None:
            total_failed += 1
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        if len(raw_filings) == 0:
            total_no_data += 1  # Valid — company just has no relevant filings
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Parse each filing
        parsed = []
        for filing in raw_filings:
            records = parse_filing(filing, symbol, security_id, bse_code)
            parsed.extend(records)

        if not parsed:
            total_no_data += 1
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        total_succeeded += 1
        batch_buffer.extend(parsed)

        # Save in batches of 500
        if len(batch_buffer) >= 500:
            ok, fail = save_filings(supabase, batch_buffer)
            total_records += ok
            batch_buffer   = []
            log.info(f"💾 Batch saved: {ok} filings (total: {total_records})")

        # Progress every 50 stocks
        if (i + 1) % 50 == 0:
            log.info(
                f"Progress: {i+1}/{len(stocks)} stocks | "
                f"✅ {total_succeeded} with filings | "
                f"⚪ {total_no_data} no filings | "
                f"❌ {total_failed} errors | "
                f"💾 {total_records} saved"
            )

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Save remaining records
    if batch_buffer:
        ok, fail = save_filings(supabase, batch_buffer)
        total_records += ok
        log.info(f"💾 Final batch saved: {ok} filings (total: {total_records})")

    # Log run
    status = 'COMPLETED' if total_failed < total_attempted * 0.1 else 'PARTIAL'
    log_run_to_db(
        supabase, started_at,
        attempted=total_attempted,
        succeeded=total_succeeded,
        failed=total_failed,
        no_data=total_no_data,
        records_saved=total_records,
        status=status
    )

    # Summary
    duration = (datetime.utcnow() - started_at).total_seconds() / 60
    log.info("=" * 60)
    log.info("BSE Filings Pipeline — Complete")
    log.info(f"Stocks attempted:     {total_attempted}")
    log.info(f"Stocks with filings:  {total_succeeded}")
    log.info(f"Stocks no filings:    {total_no_data}  ← valid")
    log.info(f"Stocks failed:        {total_failed}   ← actual errors")
    log.info(f"Filings saved:        {total_records}")
    log.info(f"Duration:             {duration:.1f} minutes")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
