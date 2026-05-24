#!/usr/bin/env python3
"""
Valvo Daily Fundamentals Sync
==============================
Runs nightly at 23:00 IST. Pulls only TODAY's new filings from NSE, processes
them, and upserts into canonical tables.

Feeds:
  1. /integrated-filing-results  (date-filtered)  → financials + segments
  2. /corporate-share-holdings-master (bulk, client-side filter)  → shareholding
  3. /corporates-corporateActions (date-filtered)  → corporate_actions

Reuses run_pipeline_v2.process_stock() via monkey-patch of its discovery
functions — zero code duplication.

Run modes:
  python3 run_daily_sync.py             # yesterday + today
  python3 run_daily_sync.py --days 30   # wider catch-up window
"""
import os
import re
import sys
import time
import json
import logging
import argparse
from datetime import datetime, timedelta, date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_pipeline_v2 as pipe
from run_pipeline_v2 import (
    NSESession, process_stock, store_records,
    log_pipeline_failure, load_universe
)
from supabase import create_client
from dotenv import load_dotenv
load_dotenv()

# ─── Logging ───────────────────────────────────────────────────────────
LOG_DIR = os.path.expanduser("~/fundamentals")
log_file = os.path.join(LOG_DIR, f"daily_sync_{date.today().isoformat()}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── Endpoints ─────────────────────────────────────────────────────────
INTEGRATED_URL = (
    "https://www.nseindia.com/api/integrated-filing-results"
    "?index=equities&from_date={from_d}&to_date={to_d}&period=Integrated%20Filing"
)
SHAREHOLDING_URL = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities"
CORP_ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&from_date={from_d}&to_date={to_d}"
)


# ═══════════════════════════════════════════════════════════════════════
#  FETCHERS
# ═══════════════════════════════════════════════════════════════════════
def fetch_integrated_filings(nse, from_d, to_d):
    r = nse.get(INTEGRATED_URL.format(from_d=from_d, to_d=to_d))
    if not r or r.status_code != 200:
        log.error(f"Integrated feed failed: status={r.status_code if r else 'None'}")
        return []
    data = r.json().get("data", [])
    fins = [d for d in data
            if d.get("type") == "Integrated Filing- Financials"
            and d.get("xbrl")]
    log.info(f"Integrated feed: {len(data)} total records, {len(fins)} financials")
    return fins


def fetch_shareholding_bulk(nse, from_d_dt):
    r = nse.get(SHAREHOLDING_URL)
    if not r or r.status_code != 200:
        log.error(f"Shareholding feed failed: status={r.status_code if r else 'None'}")
        return []
    try:
        data = r.json()
        if not isinstance(data, list):
            data = []
    except Exception as e:
        log.error(f"Shareholding JSON parse failed: {e}")
        return []

    def _in_window(rec):
        bd = rec.get("broadcastDate") or rec.get("submissionDate")
        if not bd:
            return False
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%d-%B-%Y"):
            try:
                return datetime.strptime(bd, fmt).date() >= from_d_dt
            except ValueError:
                continue
        return False

    in_window = [rec for rec in data if _in_window(rec) and rec.get("xbrl")]
    log.info(f"Shareholding feed: {len(data)} total, {len(in_window)} in window")
    return in_window


def fetch_corporate_actions(nse, from_d, to_d):
    # Override to_d: push it 60 days forward to capture upcoming ex-dates
    # (dividends, splits, bonuses, rights, buybacks scheduled in the next 60d).
    # Without this, NSE only returns past actions where ex_date <= today.
    _to_dt = datetime.strptime(to_d, "%d-%m-%Y").date() + timedelta(days=60)
    to_d = _to_dt.strftime("%d-%m-%Y")
    r = nse.get(CORP_ACTIONS_URL.format(from_d=from_d, to_d=to_d))
    if not r or r.status_code != 200:
        log.error(f"Corporate actions feed failed: status={r.status_code if r else 'None'}")
        return []
    try:
        data = r.json()
        if not isinstance(data, list):
            data = []
    except Exception as e:
        log.error(f"Corp actions JSON parse failed: {e}")
        return []
    log.info(f"Corporate actions feed: {len(data)} records")
    return data


# ═══════════════════════════════════════════════════════════════════════
#  PARSERS
# ═══════════════════════════════════════════════════════════════════════
def parse_corporate_action_subject(subject):
    """Parse NSE 'subject' / BSE 'Purpose' text -> (action_type, div_amount, bonus_ratio, split_ratio).
    Order matters — more specific patterns checked first."""
    if not subject:
        return 'OTHER', None, None, None
    s = subject.strip()
    su = s.upper()

    # Specific multi-word patterns first
    if 'SPIN' in su and 'OFF' in su:
        return 'DEMERGER', None, None, None
    if 'DEMERGER' in su or 'SCHEME OF ARRANGEMENT' in su:
        return 'DEMERGER', None, None, None
    if 'AMALGAM' in su or ('MERGER' in su and 'DEMERGER' not in su):
        return 'MERGER', None, None, None
    if 'CONSOLIDATION' in su:
        return 'CONSOLIDATION', None, None, None
    if 'DELIST' in su:
        return 'DELISTING', None, None, None
    if 'REDUCTION OF CAPITAL' in su or 'CAPITAL REDUCTION' in su:
        return 'CAPITAL_REDUCTION', None, None, None
    if 'INCOME DISTRIBUTION' in su:
        # REIT / InvIT income — economically a dividend
        m = re.search(r'([0-9]+(?:\.[0-9]+)?)', s)
        amt = float(m.group(1)) if m else None
        return 'REIT_INCOME', amt, None, None
    if 'FACE VALUE' in su:
        return 'FACE_VALUE_CHANGE', None, None, None

    # Common types
    if 'DIVIDEND' in su:
        # Find first numeric value (handles "Rs. 6", "Rs.- 6.0000", "INR 6", "₹6", "Rs. - 0.0400" etc)
        m = re.search(r'([0-9]+(?:\.[0-9]+)?)', s)
        amt = float(m.group(1)) if m else None
        return 'DIVIDEND', amt, None, None
    if 'BONUS' in su:
        m = re.search(r'(\d+\s*:\s*\d+)', s)
        return 'BONUS', None, (m.group(1).replace(' ', '') if m else None), None
    if 'SPLIT' in su or 'SUB-DIV' in su or 'SUBDIV' in su:
        m = re.search(r'Rs\.?\s*(\d+)\S*\s*to\s*Rs\.?\s*(\d+)', s, re.IGNORECASE)
        if m:
            return 'SPLIT', None, None, f"{m.group(1)}:{m.group(2)}"
        return 'SPLIT', None, None, None
    if 'BUY' in su and 'BACK' in su:
        return 'BUYBACK', None, None, None
    if 'RIGHT' in su:
        return 'RIGHTS', None, None, None
    if 'AGM' in su or 'MEETING' in su:
        return 'MEETING', None, None, None
    return 'OTHER', None, None, None



def parse_date_flexible(s):
    if not s or s == '-':
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════
#  CORE PROCESSORS — monkey-patch process_stock's discovery functions
# ═══════════════════════════════════════════════════════════════════════
def process_financials_via_patch(stocks_filings, universe_lookup, nse, supabase):
    counts = {"stocks_processed": 0, "quarterly_rows": 0, "annual_rows": 0,
              "segment_rows": 0, "skipped_not_in_universe": 0, "errors": 0}

    orig_q = pipe.fetch_quarterly_filings
    orig_a = pipe.fetch_annual_filings
    orig_sh = pipe.fetch_shareholding

    for symbol, filings in stocks_filings.items():
        if symbol not in universe_lookup:
            log.warning(f"[{symbol}] not in universe — skipping")
            counts["skipped_not_in_universe"] += 1
            continue
        security_id, isin = universe_lookup[symbol]

        def is_q4(f):
            qe = (f.get("qe_Date") or "").upper()
            return '-MAR-' in qe or qe.endswith('MAR') or '-MARCH-' in qe

        q_filings = filings
        a_filings = [f for f in filings if is_q4(f)]

        pipe.fetch_quarterly_filings = lambda sym, n, qf=q_filings: qf
        pipe.fetch_annual_filings = lambda sym, n, af=a_filings: af
        pipe.fetch_shareholding = lambda sym, n: []

        try:
            stats = process_stock(symbol, security_id, isin, nse, supabase)
            counts["stocks_processed"] += 1
            counts["quarterly_rows"] += stats.get("quarterly", 0)
            counts["annual_rows"] += stats.get("annual", 0)
            counts["segment_rows"] += stats.get("segments", 0)
            log.info(f"[{symbol}] q={stats.get('quarterly')} a={stats.get('annual')} seg={stats.get('segments')}")
        except Exception as e:
            log.error(f"[{symbol}] process_stock raised: {e}")
            counts["errors"] += 1
            try:
                log_pipeline_failure(
                    supabase, symbol, security_id, isin,
                    stage='daily_sync_process', failure_type='exception',
                    error_message=str(e)[:500]
                )
            except Exception:
                pass

    pipe.fetch_quarterly_filings = orig_q
    pipe.fetch_annual_filings = orig_a
    pipe.fetch_shareholding = orig_sh
    return counts


def process_shareholdings(sh_records, universe_lookup, nse, supabase):
    counts = {"stocks_processed": 0, "rows": 0, "errors": 0}

    by_symbol = defaultdict(list)
    for rec in sh_records:
        sym = rec.get("symbol")
        if sym:
            by_symbol[sym].append(rec)

    orig_q = pipe.fetch_quarterly_filings
    orig_a = pipe.fetch_annual_filings
    orig_sh = pipe.fetch_shareholding

    for symbol, recs in by_symbol.items():
        if symbol not in universe_lookup:
            continue
        security_id, isin = universe_lookup[symbol]

        pipe.fetch_quarterly_filings = lambda sym, n: []
        pipe.fetch_annual_filings = lambda sym, n: []
        pipe.fetch_shareholding = lambda sym, n, r=recs: r

        try:
            stats = process_stock(symbol, security_id, isin, nse, supabase)
            counts["stocks_processed"] += 1
            counts["rows"] += stats.get("shareholding", 0)
            log.info(f"[{symbol}] shareholding={stats.get('shareholding')}")
        except Exception as e:
            log.error(f"[{symbol}] shareholding failed: {e}")
            counts["errors"] += 1

    pipe.fetch_quarterly_filings = orig_q
    pipe.fetch_annual_filings = orig_a
    pipe.fetch_shareholding = orig_sh
    return counts


def process_corporate_actions(ca_records, universe_lookup, supabase):
    counts = {"rows": 0, "skipped_not_in_universe": 0}
    rows = []

    for rec in ca_records:
        symbol = rec.get("symbol")
        if not symbol or symbol not in universe_lookup:
            counts["skipped_not_in_universe"] += 1
            continue
        security_id, _ = universe_lookup[symbol]

        subject = rec.get("subject", "")
        action_type, div_amt, bonus_r, split_r = parse_corporate_action_subject(subject)
        ex_date = parse_date_flexible(rec.get("exDate"))
        if not ex_date:
            continue

        rows.append({
            "security_id": security_id,
            "symbol": symbol,
            "bse_code": None,
            "action_type": action_type,
            "action_date": parse_date_flexible(rec.get("caBroadcastDate")) or ex_date,
            "ex_date": ex_date,
            "record_date": parse_date_flexible(rec.get("recDate")),
            "payment_date": None,
            "details": subject,
            "dividend_amount": div_amt,
            "dividend_type": ("Interim" if "INTERIM" in subject.upper()
                              else ("Final" if "FINAL" in subject.upper() else None)),
            "bonus_ratio": bonus_r,
            "split_ratio": split_r,
            "face_value_before": None,
            "face_value_after": None,
            "source_url": None,
        })

    if rows:
        counts["rows"] = store_records(
            "corporate_actions", rows,
            "symbol,action_type,ex_date", supabase
        )
    return counts


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
#  FEED 5: IPO issues (upcoming + currently bidding)
# ═══════════════════════════════════════════════════════════════════════
IPO_UPCOMING_URL = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
IPO_CURRENT_URL  = "https://www.nseindia.com/api/ipo-current-issue"

_PRICE_BAND = re.compile(r"Rs\.?\s*(\d+(?:\.\d+)?)\s*(?:to|-|–)\s*Rs?\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _parse_ipo_date(raw):
    """NSE sends '17-Apr-2026' or '17-APR-2026'. Return YYYY-MM-DD or None."""
    if not raw or str(raw).strip() in ("-", ""):
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_price_band(raw):
    """'Rs.99 to Rs.100' -> (99.0, 100.0). Returns (min, max) or (None, None)."""
    if not raw:
        return None, None
    m = _PRICE_BAND.search(str(raw))
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except (ValueError, TypeError):
        return None, None


def fetch_ipo_issues(nse):
    """Pull both upcoming + currently-bidding IPO feeds. Returns combined list with source tag."""
    combined = []
    for label, url in (("upcoming", IPO_UPCOMING_URL), ("current", IPO_CURRENT_URL)):
        r = nse.get(url)
        if not r or r.status_code != 200:
            log.error(f"IPO {label} feed failed: status={r.status_code if r else 'None'}")
            continue
        try:
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            log.error(f"IPO {label} JSON parse failed: {e}")
            continue
        for rec in items:
            rec["_source"] = label
            combined.append(rec)
        log.info(f"IPO {label} feed: {len(items)} records")
    return combined


def process_ipo_issues(records, supabase):
    """Parse raw NSE records -> upsert into ipo_issues."""
    counts = {"rows_saved": 0, "skipped": 0}
    rows = []
    for rec in records:
        symbol = (rec.get("symbol") or "").strip()
        start_date = _parse_ipo_date(rec.get("issueStartDate"))
        if not symbol or not start_date:
            counts["skipped"] += 1
            continue
        price_raw = rec.get("issuePrice") or ""
        p_min, p_max = _parse_price_band(price_raw)
        rows.append({
            "symbol":           symbol,
            "company_name":     (rec.get("companyName") or "").strip() or None,
            "series":           (rec.get("series") or "").strip() or None,
            "issue_start_date": start_date,
            "issue_end_date":   _parse_ipo_date(rec.get("issueEndDate")),
            "price_band_raw":   price_raw.strip() or None,
            "price_band_min":   p_min,
            "price_band_max":   p_max,
            "issue_size":       (rec.get("issueSize") or "").strip() or None,
            "status":           (rec.get("status") or "").strip() or None,
            "category":         "IPO",
            "source_endpoint":  rec.get("_source"),
        })
    if not rows:
        return counts

    # Dedup within this batch by (symbol, issue_start_date) — same IPO may
    # appear in both feeds when it's currently open for bidding.
    _dedup = {}
    for r in rows:
        key = (r["symbol"], r["issue_start_date"])
        _dedup[key] = r
    rows = list(_dedup.values())

    try:
        (supabase.table("ipo_issues")
            .upsert(rows, on_conflict="symbol,issue_start_date")
            .execute())
        counts["rows_saved"] = len(rows)
    except Exception as e:
        log.error(f"ipo_issues upsert failed: {str(e)[:200]}")
    return counts



# ═══════════════════════════════════════════════════════════════════════
#  FEED 6: Market holidays (trading + clearing, all segments)
# ═══════════════════════════════════════════════════════════════════════
HOLIDAY_TRADING_URL  = "https://www.nseindia.com/api/holiday-master?type=trading"
HOLIDAY_CLEARING_URL = "https://www.nseindia.com/api/holiday-master?type=clearing"


def _parse_holiday_date(raw):
    """NSE sends '15-Jan-2026' or '15-JAN-2026'. Return YYYY-MM-DD or None."""
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def fetch_market_holidays(nse):
    """Pull trading + clearing holiday calendars. Returns list of tagged rows."""
    combined = []
    for htype, url in (("trading", HOLIDAY_TRADING_URL), ("clearing", HOLIDAY_CLEARING_URL)):
        r = nse.get(url)
        if not r or r.status_code != 200:
            log.error(f"Holidays {htype} feed failed: status={r.status_code if r else 'None'}")
            continue
        try:
            data = r.json()
        except Exception as e:
            log.error(f"Holidays {htype} JSON parse failed: {e}")
            continue
        if not isinstance(data, dict):
            log.error(f"Holidays {htype}: unexpected payload shape {type(data)}")
            continue
        seg_count = 0
        for segment, rows in data.items():
            if not isinstance(rows, list):
                continue
            for rec in rows:
                rec = dict(rec)  # don't mutate original
                rec["_segment"] = segment
                rec["_type"] = htype
                combined.append(rec)
                seg_count += 1
        log.info(f"Holidays {htype} feed: {seg_count} rows across {len(data)} segments")
    return combined


def process_market_holidays(records, supabase):
    """Parse + upsert holiday rows. Keyed on (holiday_date, holiday_type, segment)."""
    counts = {"rows_saved": 0, "skipped": 0}
    rows = []
    for rec in records:
        hdate = _parse_holiday_date(rec.get("tradingDate"))
        if not hdate:
            counts["skipped"] += 1
            continue
        rows.append({
            "holiday_date":     hdate,
            "weekday":          (rec.get("weekDay") or "").strip() or None,
            "description":      (rec.get("description") or "").strip() or None,
            "holiday_type":     rec["_type"],
            "segment":          rec["_segment"],
            "morning_session":  rec.get("morning_session"),
            "evening_session":  rec.get("evening_session"),
            "source_endpoint":  f"holiday-master?type={rec['_type']}",
        })
    if not rows:
        return counts

    # Dedup within batch on (date, type, segment)
    _dedup = {}
    for r in rows:
        key = (r["holiday_date"], r["holiday_type"], r["segment"])
        _dedup[key] = r
    rows = list(_dedup.values())

    # Upsert in batches of 200
    BATCH = 200
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        try:
            (supabase.table("market_holidays")
                .upsert(batch, on_conflict="holiday_date,holiday_type,segment")
                .execute())
            counts["rows_saved"] += len(batch)
        except Exception as e:
            log.error(f"market_holidays upsert failed at batch {i}: {str(e)[:200]}")
    return counts



# ===================================================================
#  FEED 7: BSE Corporate Actions (DefaultData endpoint)
# ===================================================================
BSE_CA_URL = "https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w?Fdate={f}&TDate={t}&strCat=-1&strType=0"
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}


def fetch_bse_corporate_actions(from_d, to_d):
    """from_d/to_d are date objects. Returns list of BSE corp action records."""
    import requests
    f = from_d.strftime("%Y%m%d")
    t = to_d.strftime("%Y%m%d")
    url = BSE_CA_URL.format(f=f, t=t)
    try:
        r = requests.get(url, headers=BSE_HEADERS, timeout=20)
    except Exception as e:
        log.error(f"BSE corp actions request failed: {e}")
        return []
    if r.status_code != 200:
        log.error(f"BSE corp actions HTTP {r.status_code}")
        return []
    if "error_Bse" in r.url:
        log.error("BSE blocked us (error_Bse redirect)")
        return []
    try:
        data = r.json()
    except Exception as e:
        log.error(f"BSE corp actions parse failed: {e}")
        return []
    rows = data if isinstance(data, list) else data.get("Table", [])
    log.info(f"BSE corporate actions feed: {len(rows)} records")
    return rows


def _bse_parse_date(s):
    if not s or s == "-" or str(s).strip() == "":
        return None
    s = str(s).strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y%m%d", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def process_bse_corporate_actions(records, supabase):
    """Parse BSE rows, resolve symbol via bse_company_master, upsert into corporate_actions."""
    counts = {"rows": 0, "skipped_no_date": 0, "skipped_no_symbol": 0}

    bse_map = {}
    try:
        offset = 0
        page_size = 1000
        while True:
            resp = (supabase.table("bse_company_master")
                    .select("bse_code,security_id,symbol")
                    .range(offset, offset + page_size - 1)
                    .execute())
            batch = resp.data or []
            for row in batch:
                code = str(row.get("bse_code") or "").strip()
                if code:
                    bse_map[code] = (row.get("security_id"), row.get("symbol"))
            if len(batch) < page_size:
                break
            offset += page_size
        log.info(f"BSE feed: loaded {len(bse_map)} bse_code mappings")
    except Exception as e:
        log.warning(f"bse_company_master lookup failed (non-fatal): {e}")

    rows_out = []
    for rec in records:
        ex_date = _bse_parse_date(rec.get("Ex_date") or rec.get("exdate"))
        if not ex_date:
            counts["skipped_no_date"] += 1
            continue

        bse_code = str(rec.get("scrip_code") or "").strip()
        bse_short = (rec.get("short_name") or "").strip()

        security_id, nse_symbol = bse_map.get(bse_code, (None, None))
        if not nse_symbol:
            nse_symbol = bse_short or None
        if not nse_symbol:
            counts["skipped_no_symbol"] += 1
            continue

        purpose = rec.get("Purpose", "") or ""
        action_type, div_amt, bonus_r, split_r = parse_corporate_action_subject(purpose)

        rows_out.append({
            "security_id": security_id,
            "symbol": nse_symbol,
            "bse_code": bse_code or None,
            "action_type": action_type,
            "action_date": ex_date,
            "ex_date": ex_date,
            "record_date": _bse_parse_date(rec.get("RD_Date")),
            "payment_date": _bse_parse_date(rec.get("payment_date")),
            "details": purpose.strip(),
            "dividend_amount": div_amt,
            "dividend_type": ("Interim" if "INTERIM" in purpose.upper()
                              else ("Final" if "FINAL" in purpose.upper() else None)),
            "bonus_ratio": bonus_r,
            "split_ratio": split_r,
            "face_value_before": None,
            "face_value_after": None,
            "source_url": None,
            "source": "BSE",
        })

    if rows_out:
        _dedup = {}
        for r in rows_out:
            key = (r["symbol"], r["action_type"], r["ex_date"])
            _dedup[key] = r
        rows_out = list(_dedup.values())
        counts["rows"] = store_records(
            "corporate_actions", rows_out,
            "symbol,action_type,ex_date", supabase
        )
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1,
                        help="Lookback window in days (default 1 = yesterday+today)")
    parser.add_argument("--skip-shareholding", action="store_true",
                        help="Skip shareholding feed (daily mode)")
    parser.add_argument("--only-shareholding", action="store_true",
                        help="Run ONLY shareholding feed (weekly mode)")
    args = parser.parse_args()

    today = date.today()
    from_dt = today - timedelta(days=args.days)
    FROM = from_dt.strftime("%d-%m-%Y")
    TO = today.strftime("%d-%m-%Y")

    log.info(f"═══ Daily sync started ═══")
    log.info(f"Window: {FROM} → {TO} ({args.days}-day lookback)")

    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
    if not (SUPABASE_URL and SUPABASE_KEY):
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in env")
        sys.exit(1)
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    nse = NSESession()

    universe = load_universe(supabase)
    universe_lookup = {u["symbol"]: (u["security_id"], u.get("isin")) for u in universe}
    log.info(f"Universe loaded: {len(universe_lookup)} stocks")

    # ─── Feed 1: Integrated financials + segments ───
    fin_stats = {"stocks_processed": 0, "quarterly_rows": 0, "annual_rows": 0,
                 "segment_rows": 0, "errors": 0, "skipped_not_in_universe": 0}
    if not args.only_shareholding:
        log.info("── Fetching integrated filings feed ──")
        int_records = fetch_integrated_filings(nse, FROM, TO)
        filings_by_symbol = defaultdict(list)
        for rec in int_records:
            sym = rec.get("symbol")
            if sym:
                filings_by_symbol[sym].append(rec)
        log.info(f"Unique stocks with financials filings: {len(filings_by_symbol)}")

        fin_stats = process_financials_via_patch(
            filings_by_symbol, universe_lookup, nse, supabase
        )
    else:
        log.info("Skipping financials feed (--only-shareholding)")

    # ─── Feed 2: Shareholding ───
    sh_stats = {"stocks_processed": 0, "rows": 0, "errors": 0}
    if not args.skip_shareholding:
        log.info("── Fetching shareholding feed ──")
        sh_records = fetch_shareholding_bulk(nse, from_dt)
        sh_stats = process_shareholdings(sh_records, universe_lookup, nse, supabase)
    else:
        log.info("Skipping shareholding feed (--skip-shareholding)")

    # ─── Feed 3: Corporate actions ───
    ca_stats = {"rows": 0, "skipped_not_in_universe": 0}
    if not args.only_shareholding:
        log.info("── Fetching corporate actions feed ──")
        ca_records = fetch_corporate_actions(nse, FROM, TO)
        ca_stats = process_corporate_actions(ca_records, universe_lookup, supabase)
    else:
        log.info("Skipping corporate actions feed (--only-shareholding)")

    # ─── Feed 4: Board meetings (upcoming result dates) ───
    bm_stats = {"rows_saved": 0, "skipped_no_date": 0, "skipped_not_in_universe": 0}
    if not args.only_shareholding:
        log.info("── Fetching board meetings feed ──")
        # Wider window — we want upcoming meetings up to ~90 days out
        BM_TO = (today + timedelta(days=90)).strftime("%d-%m-%Y")
        bm_records = fetch_board_meetings(nse, FROM, BM_TO)
        bm_stats = process_board_meetings(bm_records, universe_lookup, supabase)
    else:
        log.info("Skipping board meetings feed (--only-shareholding)")

    # ─── Feed 5: IPO issues (upcoming + currently bidding) ───
    ipo_stats = {"rows_saved": 0, "skipped": 0}
    if not args.only_shareholding:
        log.info("── Fetching IPO issues feed ──")
        ipo_records = fetch_ipo_issues(nse)
        ipo_stats = process_ipo_issues(ipo_records, supabase)
    else:
        log.info("Skipping IPO feed (--only-shareholding)")

    # ─── Feed 6: Market holidays (trading + clearing, all segments) ───
    hol_stats = {"rows_saved": 0, "skipped": 0}
    if not args.only_shareholding:
        log.info("── Fetching market holidays feed ──")
        hol_records = fetch_market_holidays(nse)
        hol_stats = process_market_holidays(hol_records, supabase)
    else:
        log.info("Skipping holidays feed (--only-shareholding)")

    # ─── Summary ───
    # Feed 7: BSE corporate actions (60-day forward window)
    bse_ca_stats = {"rows": 0, "skipped_no_date": 0, "skipped_no_symbol": 0}
    if not args.only_shareholding:
        log.info("-- Fetching BSE corporate actions feed --")
        bse_to = today + timedelta(days=60)
        bse_records = fetch_bse_corporate_actions(today - timedelta(days=7), bse_to)
        bse_ca_stats = process_bse_corporate_actions(bse_records, supabase)
    else:
        log.info("Skipping BSE corp actions feed (--only-shareholding)")

    log.info("═══ Daily sync complete ═══")
    log.info(f"FINANCIALS: stocks={fin_stats['stocks_processed']} "
             f"q_rows={fin_stats['quarterly_rows']} a_rows={fin_stats['annual_rows']} "
             f"seg_rows={fin_stats['segment_rows']} errors={fin_stats['errors']} "
             f"skipped={fin_stats['skipped_not_in_universe']}")
    log.info(f"SHAREHOLDING: stocks={sh_stats['stocks_processed']} rows={sh_stats['rows']} errors={sh_stats['errors']}")
    log.info(f"BOARD_MEETINGS: rows={bm_stats['rows_saved']} skipped={bm_stats['skipped_no_date']}")
    log.info(f"IPO_ISSUES: rows={ipo_stats['rows_saved']} skipped={ipo_stats['skipped']}")
    log.info(f"HOLIDAYS: rows={hol_stats['rows_saved']} skipped={hol_stats['skipped']}")
    log.info(f"CORP_ACTIONS: rows={ca_stats['rows']} skipped={ca_stats['skipped_not_in_universe']}")
    log.info(f"BSE_CORP_ACTIONS: rows={bse_ca_stats['rows']} skipped_no_date={bse_ca_stats['skipped_no_date']} skipped_no_symbol={bse_ca_stats['skipped_no_symbol']}")

    total_proc = fin_stats['stocks_processed'] + fin_stats['errors']
    error_rate = fin_stats['errors'] / max(total_proc, 1)
    if error_rate > 0.3:
        log.error(f"High error rate ({error_rate:.1%}) — exiting non-zero")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════
#  FEED 4: Forthcoming board meetings (results dates)
# ═══════════════════════════════════════════════════════════════════════
BOARD_MEETINGS_URL = (
    "https://www.nseindia.com/api/corporate-board-meetings"
    "?index=equities&from_date={from_d}&to_date={to_d}"
)


def fetch_board_meetings(nse, from_d, to_d):
    """Fetch upcoming board meetings from NSE. Returns raw list."""
    r = nse.get(BOARD_MEETINGS_URL.format(from_d=from_d, to_d=to_d))
    if not r or r.status_code != 200:
        log.error(f"Board meetings feed failed: status={r.status_code if r else 'None'}")
        return []
    try:
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data", [])
        log.info(f"Board meetings feed: {len(rows)} records")
        return rows
    except Exception as e:
        log.error(f"Board meetings JSON parse failed: {e}")
        return []


# Strip trailing "and other business matters" noise; keep the rest as-is
_PURPOSE_CLEAN = re.compile(r"\s+and\s+other\s+business\s+matters?\s*\.?\s*$", re.IGNORECASE)


def _parse_bm_date(raw):
    """NSE uses '29-May-2026' format. Return YYYY-MM-DD or None."""
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def process_board_meetings(bm_records, universe_lookup, supabase):
    """
    Parse NSE board meeting records → upsert into forthcoming_results.
    Save everything (no filter by purpose). Dedup in Postgres via UNIQUE (symbol, meeting_date, purpose).
    """
    counts = {"rows_saved": 0, "skipped_no_date": 0, "skipped_not_in_universe": 0}
    rows = []

    for rec in bm_records:
        symbol = (rec.get("bm_symbol") or "").strip()
        if not symbol:
            counts["skipped_no_date"] += 1
            continue

        meeting_date = _parse_bm_date(rec.get("bm_date"))
        if not meeting_date:
            counts["skipped_no_date"] += 1
            continue

        # Link to universe (security_id); non-universe symbols still save with NULL security_id
        universe = universe_lookup.get(symbol)
        if universe:
            security_id, _isin = universe
        else:
            security_id = None
            # not blocking — we still want the row visible for future holdings

        raw_purpose = (rec.get("bm_desc") or rec.get("bm_purpose") or "").strip() or None
        purpose_label = (rec.get("bm_purpose") or "").strip() or None
        if purpose_label:
            purpose_label = _PURPOSE_CLEAN.sub("", purpose_label).strip() or None

        rows.append({
            "bse_code":     None,  # NSE source doesn't provide this
            "short_name":   None,
            "long_name":    (rec.get("sm_name") or "").strip() or None,
            "security_id":  security_id,
            "symbol":       symbol,
            "meeting_date": meeting_date,
            "purpose":      purpose_label,
            "raw_purpose":  raw_purpose,
        })

    # Dedup within this batch: NSE returns multiple records for the same
    # (symbol, date, purpose) — last one wins (likely the most recent revision).
    # Postgres can't UPSERT two rows with the same conflict key in one batch.
    _dedup = {}
    for r in rows:
        key = (r["symbol"], r["meeting_date"], r["purpose"])
        _dedup[key] = r
    rows = list(_dedup.values())

    if rows:
        # Upsert in batches of 100
        BATCH = 100
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            try:
                (supabase.table("forthcoming_results")
                    .upsert(batch, on_conflict="symbol,meeting_date,purpose")
                    .execute())
                counts["rows_saved"] += len(batch)
            except Exception as e:
                log.error(f"forthcoming_results upsert failed at batch {i}: {str(e)[:200]}")

        # Prune past meetings AFTER upsert (catches rescheduled-in-past rows too)
        try:
            from datetime import date as _d
            cutoff = _d.today().isoformat()
            supabase.table("forthcoming_results").delete().lt("meeting_date", cutoff).execute()
            log.info(f"Pruned past meetings (< {cutoff})")
        except Exception as e:
            log.warning(f"Prune failed (non-fatal): {e}")

    return counts


if __name__ == "__main__":
    main()
