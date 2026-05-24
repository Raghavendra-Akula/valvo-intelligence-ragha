"""
=============================================================
VALVO INTELLIGENCE — FUNDAMENTALS PIPELINE
Script 02: BSE Corporate Actions
=============================================================

WHAT THIS SCRIPT DOES:
    Fetches full history of corporate actions for all BSE stocks:
    - Dividends (amount, ex-date, record date, payment date)
    - Bonus issues (ratio, dates)
    - Stock splits (ratio, dates)
    - Rights issues
    - Buybacks

DATA SOURCE:
    BSE Corporate Actions API — confirmed working
    https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w
    ?scripcode={bse_code}&Flag=C

    Returns 3 tables. We use Table2 — most complete:
    Ex_date, purpose, purpose_code, Details, BCRD, PAYMENT_DATE

WHEN IT RUNS:
    Nightly — corporate actions announced any day

HOW TO RUN MANUALLY:
    python3 02_bse_corporate_actions.py

=============================================================
"""

import re                          # FIX 2: moved to top of file
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
        logging.FileHandler(LOG_DIR / 'bse_corporate_actions.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

BSE_CORP_ACTIONS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w"
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

DELAY_BETWEEN_REQUESTS = 0.3

# BSE purpose_code → our action_type mapping
# Confirmed from real Reliance API response
PURPOSE_CODE_MAP = {
    'DP': 'DIVIDEND',
    'BN': 'BONUS',
    'SP': 'SPLIT',
    'RT': 'RIGHTS',
    'BB': 'BUYBACK',
    'AG': 'AGM',
    'EG': 'EGM',
    'OT': 'OTHER',
}


# ------------------------------------------------------------------
# CONNECT TO SUPABASE
# ------------------------------------------------------------------
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------------------------------------------
# LOAD ALL BSE STOCKS WITH PAGINATION
# ------------------------------------------------------------------
def get_stocks(supabase: Client) -> list:
    """
    Returns all active equity stocks from bse_company_master.
    Paginates in chunks of 1000 — Supabase default limit is 1000 rows.
    """
    log.info("Loading stocks from bse_company_master...")

    all_stocks = []
    page       = 0
    PAGE_SIZE  = 1000

    while True:
        result = supabase.table('bse_company_master')\
            .select('bse_code, symbol, security_id')\
            .eq('status', 'ACTIVE')\
            .eq('segment', 'Equity')\
            .range(page * PAGE_SIZE, (page + 1) * PAGE_SIZE - 1)\
            .execute()

        batch = [
            {
                'bse_code':    r['bse_code'],
                'symbol':      r['symbol'] or r['bse_code'],
                'security_id': r['security_id']
            }
            for r in result.data
            if r.get('bse_code')
        ]

        all_stocks.extend(batch)
        log.info(f"Loaded page {page + 1}: {len(batch)} stocks "
                 f"(total so far: {len(all_stocks)})")

        if len(result.data) < PAGE_SIZE:
            break

        page += 1

    log.info(f"✓ Total stocks loaded: {len(all_stocks)}")
    return all_stocks


# ------------------------------------------------------------------
# FETCH CORPORATE ACTIONS FOR ONE STOCK
# ------------------------------------------------------------------
def fetch_corporate_actions(bse_code: str) -> list | None:
    """
    Fetches all corporate actions for one BSE code.

    Returns:
        list  → records fetched (may be empty if company has none)
        None  → fetch failed (network/API error)

    WHY DISTINGUISH None vs []:
        [] = company has no corporate actions (valid, not a failure)
        None = we couldn't fetch (network error, actual failure)

    We use Table2 — most complete with all action types:
        Table  = dividends only
        Table1 = bonus/splits only
        Table2 = everything combined (ex_date + purpose_code + details)
    """
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                BSE_CORP_ACTIONS_URL,
                params={'scripcode': bse_code, 'Flag': 'C'},
                headers=HEADERS,
                timeout=15
            )
            response.raise_for_status()

            # Check if response is actually JSON
            if not response.text.strip().startswith('{'):
                log.debug(f"Non-JSON response for {bse_code}")
                return None  # Actual failure — BSE didn't serve JSON

            data = response.json()

            # Table2 has everything combined
            records = data.get('Table2', [])

            # Return [] if company has no actions — this is valid
            return records if records else []

        except requests.exceptions.Timeout:
            log.debug(f"Timeout for {bse_code} (attempt {attempt})")
            if attempt < MAX_RETRIES:
                time.sleep(3)

        except requests.exceptions.HTTPError as e:
            log.debug(f"HTTP error for {bse_code}: {e}")
            return None  # Actual failure

        except Exception as e:
            log.debug(f"Error fetching {bse_code}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2)

    return None  # All retries failed


# ------------------------------------------------------------------
# PARSE ONE CORPORATE ACTION RECORD
# ------------------------------------------------------------------
def parse_action(record: dict, symbol: str,
                 security_id: str, bse_code: str) -> dict | None:
    """
    Parses one Table2 record into our corporate_actions structure.

    Real BSE Table2 record:
    {
        "DR": 1,
        "scrip_code": 500325,
        "BCRD": "RD 14/08/2025",     ← record date
        "purpose_code": "DP ",        ← DP=dividend BN=bonus SP=split
        "short_name": "RELIANCE",
        "Ex_date": "14 Aug 2025",     ← ex-date (most important)
        "purpose": "Final Dividend",  ← description
        "Details": "5.50",            ← amount for dividends
        "PAYMENT_DATE": null
    }
    """
    try:
        # Ex-date — most important field, skip record if missing
        ex_date = parse_bse_date(record.get('Ex_date', ''))
        if not ex_date:
            return None

        # Purpose code — strip trailing space BSE sends ("DP ")
        purpose_code = str(record.get('purpose_code', '')).strip()
        action_type  = PURPOSE_CODE_MAP.get(purpose_code, 'OTHER')

        # Full description
        purpose = str(record.get('purpose', '')).strip()

        # FALLBACK: If action_type is OTHER, try to detect from description text
        # BSE uses many purpose codes we didn't map — detect from details string
        if action_type == 'OTHER':
            purpose_lower = purpose.lower()
            if 'split' in purpose_lower and 'mutual fund' not in purpose_lower:
                action_type = 'SPLIT'
            elif 'right issue' in purpose_lower or 'rights issue' in purpose_lower:
                action_type = 'RIGHTS'
            elif 'buy back' in purpose_lower or 'buyback' in purpose_lower:
                action_type = 'BUYBACK'
            elif 'bonus' in purpose_lower:
                action_type = 'BONUS'
            elif 'dividend' in purpose_lower:
                action_type = 'DIVIDEND' 

        # Record date — BSE sends "RD 14/08/2025" format
        record_date = parse_bcrd(str(record.get('BCRD', '')).strip())

        # Payment date
        payment_date = parse_bse_date(record.get('PAYMENT_DATE', ''))

        # FIX 4: Use record_date as action_date fallback
        # BSE doesn't send a separate announcement date
        action_date = record_date or ex_date

        # Details field — dividend amount or bonus ratio
        details_raw = record.get('Details')
        details_str = str(details_raw).strip() if details_raw else None

        # Dividend specific fields
        dividend_amount = None
        dividend_type   = None
        if action_type == 'DIVIDEND':
            dividend_amount = safe_float(details_str)
            purpose_lower   = purpose.lower()
            if 'interim' in purpose_lower:
                dividend_type = 'INTERIM'
            elif 'special' in purpose_lower:
                dividend_type = 'SPECIAL'
            else:
                dividend_type = 'FINAL'

        # Bonus ratio — extracted from purpose string
        bonus_ratio = None
        if action_type == 'BONUS':
            bonus_ratio = extract_ratio(purpose) or details_str

        # Split ratio — extracted from purpose string
        split_ratio = None
        if action_type == 'SPLIT':
            split_ratio = extract_ratio(purpose) or details_str

        return {
            'security_id':      security_id,
            'symbol':           symbol,
            'bse_code':         bse_code,
            'action_type':      action_type,
            'action_date':      action_date,    # FIX 4: now populated
            'ex_date':          ex_date,
            'record_date':      record_date,
            'payment_date':     payment_date,
            'details':          purpose,
            'dividend_amount':  dividend_amount,
            'dividend_type':    dividend_type,
            'bonus_ratio':      bonus_ratio,
            'split_ratio':      split_ratio,
            'source_url': (
                f"{BSE_CORP_ACTIONS_URL}"
                f"?scripcode={bse_code}&Flag=C"
            ),
        }

    except Exception as e:
        log.debug(f"Error parsing action for {symbol}: {e}")
        return None


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def parse_bse_date(date_str) -> str | None:
    """Converts BSE date strings to YYYY-MM-DD."""
    if not date_str or str(date_str).strip() in ('', 'None', 'null'):
        return None
    date_str = str(date_str).strip()
    formats = [
        "%d %b %Y",   # 14 Aug 2025
        "%d-%b-%Y",   # 14-Aug-2025
        "%d/%m/%Y",   # 14/08/2025
        "%Y-%m-%d",   # 2025-08-14
        "%d %B %Y",   # 14 August 2025
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    log.debug(f"Could not parse date: {date_str}")
    return None


def parse_bcrd(bcrd_str: str) -> str | None:
    """Parses "RD 14/08/2025" → "2025-08-14"."""
    if not bcrd_str:
        return None
    cleaned = bcrd_str.replace('RD ', '').replace('RD', '').strip()
    return parse_bse_date(cleaned)


def extract_ratio(text: str) -> str | None:
    """Extracts ratio like '1:1' or '2:1' from a string."""
    # FIX 2: re is now imported at top — no re-import needed here
    match = re.search(r'\d+:\d+', text)
    return match.group(0) if match else None


def safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip().replace(',', ''))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# SAVE TO SUPABASE
# ------------------------------------------------------------------
def save_actions(supabase: Client, records: list) -> tuple[int, int]:
    """
    Saves corporate action records in batches.
    Upserts on (bse_code, action_type, ex_date).

    DEDUPLICATION:
    BSE sometimes returns duplicate records for the same action.
    Two rows with identical (bse_code, action_type, ex_date) in the
    same batch causes PostgreSQL to crash with:
    "ON CONFLICT DO UPDATE command cannot affect row a second time"
    Fix: deduplicate within each batch using a dict keyed on unique key.
    """
    if not records:
        return 0, 0

    # Deduplicate entire records list before batching
    # Keep last occurrence of each unique (bse_code, action_type, ex_date)
    seen     = {}
    for record in records:
        key = (
            record.get('bse_code', ''),
            record.get('action_type', ''),
            record.get('ex_date', '')
        )
        seen[key] = record  # Later record overwrites earlier duplicate

    deduped  = list(seen.values())
    removed  = len(records) - len(deduped)
    if removed > 0:
        log.debug(f"Removed {removed} duplicate records before saving")

    BATCH_SIZE = 100
    success    = 0
    failed     = 0

    for i in range(0, len(deduped), BATCH_SIZE):
        batch = deduped[i: i + BATCH_SIZE]
        try:
            supabase.table('corporate_actions').upsert(
                batch,
                on_conflict='bse_code,action_type,ex_date'
            ).execute()
            success += len(batch)
        except Exception as e:
            failed += len(batch)
            log.error(f"Error saving batch at {i}: {e}")

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
            'run_date':               date.today().isoformat(),
            'run_type':               'BSE_CORPORATE_ACTIONS',
            'companies_attempted':    attempted,
            'companies_succeeded':    succeeded,
            'companies_failed':       failed,
            'corporate_actions_added': records_saved,
            'started_at':             started_at.isoformat(),
            'completed_at':           completed_at.isoformat(),
            'duration_minutes':       round(duration, 2),
            'status':                 status,
            'error_message':          error_msg,
            'notes': f"{no_data} companies had no corporate actions (valid)",
        }).execute()
    except Exception as e:
        log.error(f"Could not write to pipeline_logs: {e}")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    started_at = datetime.utcnow()
    log.info("=" * 60)
    log.info("BSE Corporate Actions Pipeline — Starting")
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
    stocks = get_stocks(supabase)
    if not stocks:
        log.error("No stocks found in bse_company_master")
        return

    # Process each stock
    total_attempted = 0
    total_succeeded = 0
    total_failed    = 0
    total_no_data   = 0   # FIX 3: separate counter for valid empty responses
    total_records   = 0
    batch_buffer    = []

    for i, stock in enumerate(stocks):
        bse_code    = stock['bse_code']
        symbol      = stock['symbol']
        security_id = stock['security_id']

        total_attempted += 1

        # Fetch — None means actual error, [] means no actions (valid)
        raw_records = fetch_corporate_actions(bse_code)

        # FIX 3: Distinguish actual failures from empty data
        if raw_records is None:
            total_failed += 1   # Actual fetch error
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        if len(raw_records) == 0:
            total_no_data += 1  # Valid — company just has no actions
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Parse each record
        parsed = []
        for record in raw_records:
            action = parse_action(record, symbol, security_id, bse_code)
            if action:
                parsed.append(action)

        if not parsed:
            total_no_data += 1
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        total_succeeded += 1
        batch_buffer.extend(parsed)

        # Progress log every 50 companies — no more silent waiting
        if (i + 1) % 50 == 0:
            log.info(
                f"Progress: {i+1}/{len(stocks)} stocks | "
                f"✅ {total_succeeded} with actions | "
                f"⚪ {total_no_data} no actions | "
                f"❌ {total_failed} errors | "
                f"💾 {total_records} saved"
            )

        # Save in batches of 500 records
        if len(batch_buffer) >= 500:
            ok, fail = save_actions(supabase, batch_buffer)
            total_records += ok
            batch_buffer   = []
            log.info(f"💾 Batch saved: {ok} records (total: {total_records})")

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Save remaining records
    if batch_buffer:
        ok, fail = save_actions(supabase, batch_buffer)
        total_records += ok

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
    log.info("BSE Corporate Actions Pipeline — Complete")
    log.info(f"Stocks attempted:    {total_attempted}")
    log.info(f"Stocks with actions: {total_succeeded}")
    log.info(f"Stocks no actions:   {total_no_data}  ← valid, not failures")
    log.info(f"Stocks failed:       {total_failed}   ← actual errors")
    log.info(f"Actions saved:       {total_records}")
    log.info(f"Duration:            {duration:.1f} minutes")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
