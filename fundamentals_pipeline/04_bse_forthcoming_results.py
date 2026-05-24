"""
=============================================================
VALVO INTELLIGENCE — FUNDAMENTALS PIPELINE
Script 04: BSE Forthcoming Results
=============================================================

WHAT THIS SCRIPT DOES:
    Fetches the list of upcoming board meetings (quarterly
    results, audited results, etc.) from BSE and stores them
    in the forthcoming_results table.

    This is the data you see on
      https://www.bseindia.com/corporates/Forth_Results.html
    served by the JSON API
      https://api.bseindia.com/BseIndiaAPI/api/Forthcoming_Results/w

    Rows are keyed on (bse_code, meeting_date, purpose) so
    re-running the script upserts rather than duplicates.

    Each row is linked to our stock_universe via
    bse_company_master.bse_code -> security_id when possible.
    Rows we can't link still get stored so the data is complete.

WHEN IT RUNS:
    Daily (once per day is enough — BSE updates the list as
    companies file their board-meeting notices).

HOW TO RUN MANUALLY:
    python3 04_bse_forthcoming_results.py

=============================================================
"""

import requests
import time
import logging
import os
import re
from pathlib import Path
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'bse_forthcoming_results.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

# BSE's forthcoming-results JSON endpoint. strCat=R filters to results-only
# board meetings (as opposed to AGMs, buybacks, etc.). strPrevDate and
# strToDate are DD/MM/YYYY. We pull a 90-day window forward.
BSE_FORTHCOMING_URL = "https://api.bseindia.com/BseIndiaAPI/api/Forthcoming_Results/w"

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

LOOKAHEAD_DAYS = 90

# Trim the noisier bits BSE tacks on ("and other business matters" etc.) so
# the UI can show a short label. Keep raw_purpose for the full string.
_PURPOSE_CLEAN = re.compile(r"\s+and\s+other\s+business\s+matters?\s*\.?\s*$", re.IGNORECASE)


def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_forthcoming() -> list:
    today = date.today()
    end = today + timedelta(days=LOOKAHEAD_DAYS)
    params = {
        "strCat": "R",
        "strPrevDate": today.strftime("%d/%m/%Y"),
        "strToDate": end.strftime("%d/%m/%Y"),
        "strScrip": "",
        "strSearch": "P",
    }

    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"Fetching BSE forthcoming results (attempt {attempt}/{MAX_RETRIES})...")
            resp = requests.get(BSE_FORTHCOMING_URL, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            rows = data if isinstance(data, list) else data.get('Table', []) or data.get('Data', [])
            log.info(f"✓ Fetched {len(rows)} forthcoming-result rows from BSE")
            return rows
        except Exception as e:
            log.error(f"Error fetching forthcoming results (attempt {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)

    log.error("All retries failed. Could not fetch forthcoming results.")
    return []


def parse_meeting_date(raw):
    if not raw:
        return None
    s = str(raw).strip()
    # BSE sometimes returns ISO ("2026-04-28T00:00:00"), sometimes "28 Apr 2026"
    # or "28/04/2026". Try the common forms.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    # ISO with fractional seconds / timezone — let fromisoformat have a go
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def clean_purpose(raw):
    if not raw:
        return None
    s = str(raw).strip()
    s = _PURPOSE_CLEAN.sub("", s).strip()
    return s or None


def parse_row(raw: dict) -> dict | None:
    try:
        bse_code = str(raw.get('scrip_Cd') or raw.get('SCRIP_CD') or raw.get('scripCode') or '').strip()
        if not bse_code:
            return None

        meeting_raw = (raw.get('Meeting_Dt') or raw.get('MEETING_DT')
                       or raw.get('meeting_date') or raw.get('BM_DT'))
        meeting_date = parse_meeting_date(meeting_raw)
        if not meeting_date:
            return None

        short_name = (raw.get('short_name') or raw.get('Short_Name')
                      or raw.get('SHORT_NAME') or raw.get('scrip_Name') or '').strip() or None
        long_name = (raw.get('long_name') or raw.get('LONG_NAME')
                     or raw.get('Company_Name') or '').strip() or None
        raw_purpose = (raw.get('Purpose') or raw.get('PURPOSE')
                       or raw.get('BM_DESC') or '').strip() or None

        return {
            'bse_code':     bse_code,
            'short_name':   short_name,
            'long_name':    long_name,
            'meeting_date': meeting_date.isoformat(),
            'purpose':      clean_purpose(raw_purpose),
            'raw_purpose':  raw_purpose,
        }
    except Exception as e:
        log.warning(f"Error parsing row {raw}: {e}")
        return None


def load_bse_to_security_map(supabase: Client) -> dict:
    """bse_code -> (security_id, symbol) using bse_company_master."""
    log.info("Loading bse_company_master for security_id linking...")
    try:
        result = (supabase.table('bse_company_master')
                  .select('bse_code, security_id, symbol')
                  .execute())
    except Exception as e:
        log.warning(f"bse_company_master lookup failed: {e}. Proceeding without links.")
        return {}

    mapping = {}
    for row in result.data or []:
        bse_code = str(row.get('bse_code') or '').strip()
        if not bse_code:
            continue
        mapping[bse_code] = (row.get('security_id'), row.get('symbol'))
    log.info(f"Loaded {len(mapping)} bse_code -> security_id mappings")
    return mapping


def save_to_supabase(supabase: Client, rows: list):
    BATCH = 100
    saved = 0
    failed = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i: i + BATCH]
        try:
            (supabase.table('forthcoming_results')
                .upsert(batch, on_conflict='bse_code,meeting_date,purpose')
                .execute())
            saved += len(batch)
            log.info(f"Saved batch {i // BATCH + 1}: {len(batch)} rows (total: {saved})")
            time.sleep(0.1)
        except Exception as e:
            failed += len(batch)
            log.error(f"Error saving batch at index {i}: {e}")
    return saved, failed


def prune_stale(supabase: Client):
    """Drop rows whose meeting_date is in the past — BSE drops them from the
    feed too, and stale rows would confuse the 'next result' lookup."""
    try:
        cutoff = date.today().isoformat()
        (supabase.table('forthcoming_results')
            .delete()
            .lt('meeting_date', cutoff)
            .execute())
        log.info(f"✓ Pruned rows with meeting_date < {cutoff}")
    except Exception as e:
        log.warning(f"Could not prune stale rows: {e}")


def main():
    started_at = datetime.utcnow()
    log.info("=" * 60)
    log.info("BSE Forthcoming Results Pipeline — Starting")
    log.info(f"Started at: {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log.info("=" * 60)

    try:
        supabase = get_supabase_client()
        log.info("✓ Connected to Supabase")
    except Exception as e:
        log.error(f"Cannot connect to Supabase: {e}")
        return

    raw_rows = fetch_forthcoming()
    if not raw_rows:
        log.error("No data fetched from BSE. Exiting without writes.")
        return

    bse_map = load_bse_to_security_map(supabase)

    log.info(f"Parsing {len(raw_rows)} rows...")
    parsed = []
    skipped = 0
    linked = 0
    for raw in raw_rows:
        p = parse_row(raw)
        if p is None:
            skipped += 1
            continue
        sid, sym = bse_map.get(p['bse_code'], (None, None))
        p['security_id'] = sid
        p['symbol'] = sym
        if sid:
            linked += 1
        parsed.append(p)

    log.info(f"✓ Parsed: {len(parsed)} | Linked: {linked} | Skipped: {skipped}")

    if not parsed:
        log.error("Nothing to save after parsing. Exiting.")
        return

    prune_stale(supabase)

    log.info("Saving to forthcoming_results...")
    saved, failed = save_to_supabase(supabase, parsed)

    duration = (datetime.utcnow() - started_at).total_seconds()
    log.info("=" * 60)
    log.info("BSE Forthcoming Results Pipeline — Complete")
    log.info(f"Fetched:  {len(raw_rows)}")
    log.info(f"Parsed:   {len(parsed)}")
    log.info(f"Linked:   {linked}")
    log.info(f"Saved:    {saved}")
    log.info(f"Failed:   {failed}")
    log.info(f"Duration: {duration:.1f}s")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
