"""
=============================================================
VALVO INTELLIGENCE — FUNDAMENTALS PIPELINE
Script 03-RETRY: BSE Filing Discovery — Retry Failed Companies
=============================================================

WHAT THIS SCRIPT DOES:
    Re-fetches filings ONLY for companies that failed during
    the main 03_bse_filings.py run (BSE API throttling/errors).
    
    Uses IDENTICAL fetch/parse logic as the original script.
    Only differences:
    - Finds missing companies via Supabase RPC
    - Longer delays between requests (to avoid repeat throttling)
    - Refreshes BSE session every 30 companies (not 500)

HOW TO RUN:
    cd ~/fundamentals
    source ~/venv/bin/activate
    nohup python3 03_bse_filings_retry.py > ~/logs/bse_filings_retry.log 2>&1 &
    tail -f ~/logs/bse_filings_retry.log

PREREQUISITE:
    Supabase function get_distinct_filing_bse_codes() must exist.
    (Already created via migration.)

=============================================================
"""

import re
import requests
import time
import logging
import os
import random
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
        logging.FileHandler(LOG_DIR / 'bse_filings_retry.log'),
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
FETCH_FROM_DATE = "20100101"
FETCH_TO_DATE   = datetime.now().strftime("%Y%m%d")

BSE_PAGE_SIZE  = 50     # BSE API returns 50 filings per page
DB_PAGE_SIZE   = 1000   # Supabase pagination chunk size

# RETRY-SPECIFIC: Slower than original to avoid repeat throttling
DELAY_BETWEEN_REQUESTS = 2.0   # Original: 0.3s
DELAY_BETWEEN_PAGES    = 0.5   # Original: 0.1s
SESSION_REFRESH_EVERY  = 30    # Original: 500

MAX_PAGES_PER_COMPANY  = 100

# Filing categories — IDENTICAL to original
FINANCIAL_CATEGORIES = {
    ('Result', 'Financial Results'),
    ('Board Meeting', 'Board Meeting'),
    ('AGM/EGM', 'AGM'),
    ('AGM/EGM', 'EGM'),
    ('AGM/EGM', 'Postal Ballot'),
    ('Company Update', 'Investor Presentation'),
    ('Company Update', 'Earnings Call Transcript'),
}

FILING_TYPE_MAP = {
    ('Result', 'Financial Results'):                 'QUARTERLY_RESULT',
    ('Board Meeting', 'Board Meeting'):              'BOARD_MEETING',
    ('AGM/EGM', 'AGM'):                              'AGM',
    ('AGM/EGM', 'EGM'):                             'EGM',
    ('AGM/EGM', 'Postal Ballot'):                   'AGM',
    ('Company Update', 'Investor Presentation'):     'INVESTOR_PRESENTATION',
    ('Company Update', 'Earnings Call Transcript'):  'CONCALL_TRANSCRIPT',
}

MONTH_TO_QUARTER = {
    'june':      'Q1', 'jun': 'Q1',
    'september': 'Q2', 'sep': 'Q2',
    'december':  'Q3', 'dec': 'Q3',
    'march':     'Q4', 'mar': 'Q4',
}


# ------------------------------------------------------------------
# CREATE BSE SESSION — IDENTICAL to original
# ------------------------------------------------------------------
def create_bse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    urls_to_visit = [
        "https://www.bseindia.com",
        "https://www.bseindia.com/corporates/ann.html",
    ]

    for url in urls_to_visit:
        try:
            session.get(url, timeout=30)
        except Exception as e:
            log.debug(f"Could not visit {url}: {e}")

    cookie_count = len(session.cookies)
    if cookie_count > 0:
        log.info(f"BSE session created ({cookie_count} cookies)")
    else:
        log.info("BSE session created (0 cookies — API works without them)")

    return session


# ------------------------------------------------------------------
# CONNECT TO SUPABASE
# ------------------------------------------------------------------
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Supabase credentials missing from .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------------------------------------------
# FIND MISSING COMPANIES — retry-specific logic
# ------------------------------------------------------------------
def get_missing_companies(supabase: Client) -> list:
    """
    Finds companies that are in bse_company_master (eligible groups)
    but have ZERO filings in the filings table.
    Uses Supabase RPC function for efficient DISTINCT query.
    """
    log.info("Finding companies that need retry...")

    # Step 1: Load all eligible companies (not X/Z/ZP)
    all_eligible = []
    page = 0
    while True:
        result = supabase.table('bse_company_master') \
            .select('bse_code, symbol, security_id') \
            .eq('status', 'ACTIVE') \
            .eq('segment', 'Equity') \
            .not_.in_('group_name', ['X', 'Z', 'ZP']) \
            .range(page * DB_PAGE_SIZE, (page + 1) * DB_PAGE_SIZE - 1) \
            .execute()

        if not result.data:
            break
        all_eligible.extend(result.data)
        if len(result.data) < DB_PAGE_SIZE:
            break
        page += 1

    log.info(f"Total eligible companies: {len(all_eligible)}")

    # Step 2: Get bse_codes that already have filings (via RPC)
    existing_codes = set()
    log.info("Loading existing filing bse_codes via RPC...")
    try:
        offset = 0
        while True:
            result = supabase.rpc('get_distinct_filing_bse_codes', {}) \
                .range(offset, offset + DB_PAGE_SIZE - 1) \
                .execute()
            if not result.data:
                break
            for row in result.data:
                existing_codes.add(row['bse_code'])
            if len(result.data) < DB_PAGE_SIZE:
                break
            offset += DB_PAGE_SIZE
    except Exception as e:
        log.error(f"RPC call failed: {e}")
        log.error("Make sure get_distinct_filing_bse_codes() exists in Supabase")
        return []

    log.info(f"Companies already with filings: {len(existing_codes)}")

    # Step 3: Find missing
    missing = []
    for company in all_eligible:
        if company['bse_code'] not in existing_codes:
            missing.append({
                'bse_code':    company['bse_code'],
                'symbol':      company['symbol'] or company['bse_code'],
                'security_id': company.get('security_id'),
            })

    log.info(f"Companies needing retry: {len(missing)}")
    if missing:
        log.info(f"First 5: {[m['symbol'] for m in missing[:5]]}")

    return missing


# ------------------------------------------------------------------
# FETCH FILINGS FOR ONE COMPANY — IDENTICAL to original
# ------------------------------------------------------------------
def fetch_filings_for_company(bse_code: str,
                               session: requests.Session) -> list | None:
    """
    IDENTICAL logic to original 03_bse_filings.py.
    Returns:
        list  → relevant filings found (may be empty)
        None  → actual fetch error
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
                    timeout=60
                )
                response.raise_for_status()

                if not response.text.strip().startswith('{'):
                    if retry < MAX_PAGE_RETRIES:
                        wait = retry * 5 + random.uniform(1, 3)
                        log.warning(f"  HTML response for {bse_code} page {page} "
                                    f"(retry {retry}/{MAX_PAGE_RETRIES}) — waiting {wait:.0f}s")
                        time.sleep(wait)
                        continue
                    else:
                        log.warning(f"  {bse_code}: HTML after all retries on page {page}")
                        return None if page == 1 else relevant

                data    = response.json()
                records = data.get('Table', [])

                if not records:
                    return relevant

                for rec in records:
                    cat    = (rec.get('CATEGORYNAME') or '').strip()
                    subcat = (rec.get('SUBCATNAME') or '').strip()
                    if (cat, subcat) in FINANCIAL_CATEGORIES:
                        relevant.append(rec)

                log.info(f"  Page {page}: {len(records)} records, {len(relevant)} relevant so far")
                page_success = True
                break

            except requests.exceptions.Timeout:
                log.warning(f"  Timeout for {bse_code} page {page} (retry {retry}/{MAX_PAGE_RETRIES})")
                if retry < MAX_PAGE_RETRIES:
                    time.sleep(retry * 3)
                else:
                    log.warning(f"  {bse_code}: All retries exhausted on page {page}")
                    return None if page == 1 else relevant

            except Exception as e:
                log.warning(f"  Error for {bse_code} page {page}: {type(e).__name__}: {e}")
                return None if page == 1 else relevant

        if not page_success:
            break

        page += 1
        time.sleep(DELAY_BETWEEN_PAGES)

    return relevant


# ------------------------------------------------------------------
# PARSE ONE FILING — IDENTICAL to original
# ------------------------------------------------------------------
def parse_filing(record: dict, symbol: str,
                 security_id: str, bse_code: str) -> list:
    results = []

    try:
        news_id     = record.get('NEWSID') or ''
        cat         = (record.get('CATEGORYNAME') or '').strip()
        subcat      = (record.get('SUBCATNAME') or '').strip()
        headline    = (record.get('HEADLINE') or '').strip()

        newssub_raw  = record.get('NEWSSUB') or ''
        description  = re.sub(r'<[^>]+>', ' ', newssub_raw).strip() or headline

        filing_dt   = record.get('DT_TM') or ''
        pdf_flag    = record.get('PDFFLAG', 0)
        attachment  = (record.get('ATTACHMENTNAME') or '').strip()
        xml_name    = (record.get('XML_NAME') or '').strip()

        filing_date = parse_bse_datetime(filing_dt)
        if not filing_date:
            return []

        if not news_id:
            return []

        filing_type = FILING_TYPE_MAP.get((cat, subcat), 'OTHER')
        period      = extract_period(description)
        fiscal_year = extract_fiscal_year(period)

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

        if pdf_flag and attachment:
            pdf_record = dict(base)
            pdf_record['pdf_url'] = f"{BSE_PDF_BASE}{attachment}"
            results.append(pdf_record)

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
# HELPERS — IDENTICAL to original
# ------------------------------------------------------------------
def parse_bse_datetime(dt_str) -> str | None:
    if not dt_str:
        return None
    try:
        return str(dt_str).strip()[:10]
    except Exception:
        return None


def extract_period(text: str) -> str | None:
    if not text:
        return None

    text_lower = text.lower()

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
            if month in ('april', 'may', 'june', 'july', 'august',
                         'september', 'october', 'november', 'december',
                         'apr', 'jun', 'jul', 'aug', 'sep', 'oct',
                         'nov', 'dec'):
                fy = str(year + 1)[-2:]
            else:
                fy = str(year)[-2:]

            if 'year ended' in text_lower or 'annual' in text_lower:
                return f"FY{fy}"

            return f"{quarter}FY{fy}"

    return None


def extract_fiscal_year(period: str) -> str | None:
    if not period:
        return None
    if period.startswith('FY'):
        return period
    if 'FY' in period:
        return 'FY' + period.split('FY')[1]
    return None


# ------------------------------------------------------------------
# SAVE TO SUPABASE — IDENTICAL to original
# ------------------------------------------------------------------
def save_filings(supabase: Client, records: list) -> tuple:
    if not records:
        return 0, 0

    seen = {}
    for r in records:
        key = r.get('bse_filing_id', '').strip()
        if key:
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
# MAIN
# ------------------------------------------------------------------
def main():
    started_at = datetime.utcnow()
    log.info("=" * 60)
    log.info("BSE Filings RETRY Pipeline — Starting")
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

    # Find missing companies
    stocks = get_missing_companies(supabase)
    if not stocks:
        log.info("No companies need retry. All good!")
        return

    # Create BSE session
    bse_session = create_bse_session()

    # Process each stock
    total_attempted = 0
    total_succeeded = 0
    total_failed    = 0
    total_no_data   = 0
    total_records   = 0
    batch_buffer    = []

    for i, stock in enumerate(stocks):
        bse_code    = stock['bse_code']
        symbol      = stock['symbol']
        security_id = stock['security_id']

        total_attempted += 1

        # RETRY-SPECIFIC: Refresh session more often
        if i > 0 and i % SESSION_REFRESH_EVERY == 0:
            log.info(f"Refreshing BSE session at stock {i}...")
            bse_session = create_bse_session()
            time.sleep(2)

        # Fetch
        log.info(f"[{i+1}/{len(stocks)}] Fetching {symbol} ({bse_code})...")
        raw_filings = fetch_filings_for_company(bse_code, bse_session)

        if raw_filings is None:
            total_failed += 1
            # RETRY-SPECIFIC: On failure, refresh session and retry once
            log.warning(f"  ✗ {symbol} ({bse_code}): API failure — retrying with fresh session")
            bse_session = create_bse_session()
            time.sleep(5)
            raw_filings = fetch_filings_for_company(bse_code, bse_session)
            if raw_filings is None:
                log.error(f"  ✗✗ {symbol}: Failed again after session refresh")
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue
            else:
                total_failed -= 1  # Recovered

        if len(raw_filings) == 0:
            total_no_data += 1
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Parse — IDENTICAL to original
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

        # Progress every 10 stocks
        if (i + 1) % 10 == 0:
            log.info(
                f"Progress: {i+1}/{len(stocks)} stocks | "
                f"✅ {total_succeeded} with filings | "
                f"⚪ {total_no_data} no filings | "
                f"❌ {total_failed} errors | "
                f"💾 {total_records} saved"
            )

        time.sleep(DELAY_BETWEEN_REQUESTS + random.uniform(0, 1))

    # Save remaining
    if batch_buffer:
        ok, fail = save_filings(supabase, batch_buffer)
        total_records += ok
        log.info(f"💾 Final batch saved: {ok} filings (total: {total_records})")

    # Log run
    status = 'COMPLETED' if total_failed == 0 else 'PARTIAL'
    completed_at = datetime.utcnow()
    duration = (completed_at - started_at).total_seconds() / 60
    try:
        supabase.table('pipeline_logs').insert({
            'run_date':            date.today().isoformat(),
            'run_type':            'BSE_FILINGS_RETRY',
            'companies_attempted': total_attempted,
            'companies_succeeded': total_succeeded,
            'companies_failed':    total_failed,
            'new_filings_found':   total_records,
            'started_at':          started_at.isoformat(),
            'completed_at':        completed_at.isoformat(),
            'duration_minutes':    round(duration, 2),
            'status':              status,
            'notes': f"{total_no_data} companies had no relevant filings (valid)",
        }).execute()
    except Exception as e:
        log.error(f"Could not write to pipeline_logs: {e}")

    # Summary
    log.info("=" * 60)
    log.info("BSE Filings RETRY Pipeline — Complete")
    log.info(f"Stocks attempted:     {total_attempted}")
    log.info(f"Stocks with filings:  {total_succeeded}")
    log.info(f"Stocks no filings:    {total_no_data}  ← valid")
    log.info(f"Stocks failed:        {total_failed}   ← actual errors")
    log.info(f"Filings saved:        {total_records}")
    log.info(f"Duration:             {duration:.1f} minutes")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
