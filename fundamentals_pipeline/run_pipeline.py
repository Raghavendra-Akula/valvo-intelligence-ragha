"""
VALVO Intelligence — NSE Fundamentals Pipeline
================================================
Fetches complete fundamental data from NSE for all stocks:
  1. Quarterly P&L (2020 onwards) from XBRL
  2. Segment breakdown from XBRL
  3. Annual P&L + Balance Sheet + Cash Flow from XBRL
  4. Shareholding pattern from NSE API

Initial backfill: ~14 hours for full universe
Daily sync: ~15 minutes (only new filings)

Usage:
  python3 run_pipeline.py                    # all stocks
  python3 run_pipeline.py TCS               # single stock test
  python3 run_pipeline.py TCS RELIANCE      # specific stocks

Requires: .env with SUPABASE_URL and SUPABASE_SERVICE_KEY
"""

import os
import sys
import re
import time
import json
import logging
import requests
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

IST = timezone(timedelta(hours=5, minutes=30))
INR_TO_CR = 10000000  # 1 Crore = 10^7

# NSE API rate limiting
REQUEST_DELAY = 0.5
SESSION_REFRESH_EVERY = 50
MAX_RETRIES = 3

# Date ranges for historical data (FY20 to FY25)
QUARTERLY_RANGES = [
    ("01-04-2019", "31-03-2020"),
    ("01-04-2020", "31-03-2021"),
    ("01-04-2021", "31-03-2022"),
    ("01-04-2022", "31-03-2023"),
    ("01-04-2023", "31-03-2024"),
    ("01-04-2024", "31-03-2025"),
]

ANNUAL_RANGES = [
    ("01-04-2019", "31-03-2020"),
    ("01-04-2020", "31-03-2021"),
    ("01-04-2021", "31-03-2022"),
    ("01-04-2022", "31-03-2023"),
    ("01-04-2023", "31-03-2024"),
    ("01-04-2024", "31-03-2025"),
]

# Ordinal words used in XBRL context IDs for segments
ORDINAL_PATTERN = re.compile(
    r"^(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|"
    r"Eleven|Twelve|Thirteen|Fourteen|Fifteen)Reportable"
)
ORDINAL_ORDER = {
    "One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5,
    "Six": 6, "Seven": 7, "Eight": 8, "Nine": 9, "Ten": 10,
    "Eleven": 11, "Twelve": 12, "Thirteen": 13, "Fourteen": 14, "Fifteen": 15,
}

# ═══════════════════════════════════════════════════════════════════
# XBRL TAG → DATABASE COLUMN MAPPING
# ═══════════════════════════════════════════════════════════════════

QUARTERLY_TAG_MAP = {
    "RevenueFromOperations": ("revenue_cr", True),
    "OtherIncome": ("other_income_cr", True),
    "Income": ("total_income_cr", True),
    "CostOfMaterialsConsumed": ("raw_material_cost_cr", True),
    "PurchasesOfStockInTrade": ("purchases_stock_in_trade_cr", True),
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": ("changes_in_inventories_cr", True),
    "EmployeeBenefitExpense": ("employee_cost_cr", True),
    "FinanceCosts": ("interest_cr", True),
    "DepreciationDepletionAndAmortisationExpense": ("depreciation_cr", True),
    "OtherExpenses": ("other_expenses_cr", True),
    "Expenses": ("expenses_cr", True),
    "ProfitBeforeExceptionalItemsAndTax": ("pbt_before_exceptional_cr", True),
    "ExceptionalItemsBeforeTax": ("exceptional_items_cr", True),
    "ProfitBeforeTax": ("profit_before_tax_cr", True),
    "CurrentTax": ("current_tax_cr", True),
    "DeferredTax": ("deferred_tax_cr", True),
    "TaxExpense": ("tax_cr", True),
    "ProfitLossForPeriod": ("net_profit_cr", True),
    "ProfitOrLossAttributableToOwnersOfParent": ("adjusted_net_profit_cr", True),
    "ProfitOrLossAttributableToNonControllingInterests": ("minority_interest_cr", True),
    "OtherComprehensiveIncomeNetOfTaxes": ("other_comprehensive_income_cr", True),
    "ComprehensiveIncomeForThePeriod": ("comprehensive_income_cr", True),
    "PaidUpValueOfEquityShareCapital": ("paid_up_capital_cr", True),
    "FaceValueOfEquityShareCapital": ("face_value", False),
    "BasicEarningsLossPerShareFromContinuingOperations": ("eps", False),
    "DilutedEarningsLossPerShareFromContinuingOperations": ("eps_diluted", False),
    "DebtEquityRatio": ("debt_equity_ratio", False),
    "DebtServiceCoverageRatio": ("debt_service_coverage_ratio", False),
    "InterestServiceCoverageRatio": ("interest_service_coverage_ratio", False),
}

# Banking Format XBRL Tags (values already in Crores, NOT Rupees)
BANKING_TAG_MAP = {
    "InterestEarned": ("revenue_cr", False),
    "OtherIncome": ("other_income_cr", False),
    "Income": ("total_income_cr", False),
    "InterestExpended": ("interest_cr", False),
    "EmployeesCost": ("employee_cost_cr", False),
    "OtherOperatingExpenses": ("other_expenses_cr", False),
    "OperatingExpenses": ("expenses_cr", False),
    "OperatingProfitBeforeProvisionAndContingencies": ("operating_profit_cr", False),
    "ProvisionsOtherThanTaxAndContingencies": ("depreciation_cr", False),
    "ExceptionalItems": ("exceptional_items_cr", False),
    "ProfitLossFromOrdinaryActivitiesBeforeTax": ("profit_before_tax_cr", False),
    "TaxExpense": ("tax_cr", False),
    "ProfitLossForThePeriod": ("net_profit_cr", False),
    "ProfitLossOfMinorityInterest": ("minority_interest_cr", False),
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": ("adjusted_net_profit_cr", False),
    "PaidUpValueOfEquityShareCapital": ("paid_up_capital_cr", False),
    "FaceValueOfEquityShareCapital": ("face_value", False),
    "BasicEarningsPerShareBeforeExtraordinaryItems": ("eps", False),
    "DilutedEarningsPerShareBeforeExtraordinaryItems": ("eps_diluted", False),
}

# Banking Format XBRL Tags (values already in Crores, NOT Rupees)
BANKING_TAG_MAP = {
    "InterestEarned": ("revenue_cr", False),
    "OtherIncome": ("other_income_cr", False),
    "Income": ("total_income_cr", False),
    "InterestExpended": ("interest_cr", False),
    "EmployeesCost": ("employee_cost_cr", False),
    "OtherOperatingExpenses": ("other_expenses_cr", False),
    "OperatingExpenses": ("expenses_cr", False),
    "OperatingProfitBeforeProvisionAndContingencies": ("operating_profit_cr", False),
    "ProvisionsOtherThanTaxAndContingencies": ("depreciation_cr", False),
    "ExceptionalItems": ("exceptional_items_cr", False),
    "ProfitLossFromOrdinaryActivitiesBeforeTax": ("profit_before_tax_cr", False),
    "TaxExpense": ("tax_cr", False),
    "ProfitLossForThePeriod": ("net_profit_cr", False),
    "ProfitLossOfMinorityInterest": ("minority_interest_cr", False),
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": ("adjusted_net_profit_cr", False),
    "PaidUpValueOfEquityShareCapital": ("paid_up_capital_cr", False),
    "FaceValueOfEquityShareCapital": ("face_value", False),
    "BasicEarningsPerShareBeforeExtraordinaryItems": ("eps", False),
    "DilutedEarningsPerShareBeforeExtraordinaryItems": ("eps_diluted", False),
}

ANNUAL_BS_TAG_MAP = {
    "Assets": ("total_assets_cr", True),
    "NoncurrentAssets": ("other_assets_cr", True),
    "CurrentAssets": ("total_current_assets_cr", True),
    "Equity": ("total_equity_cr", True),
    "EquityShareCapital": ("equity_capital_cr", True),
    "OtherEquity": ("reserves_cr", True),
    "Liabilities": ("total_liabilities_cr", True),
    "NoncurrentLiabilities": ("other_liabilities_cr", True),
    "CurrentLiabilities": ("total_current_liabilities_cr", True),
    "BorrowingsNoncurrent": ("long_term_borrowings_cr", True),
    "BorrowingsCurrent": ("short_term_borrowings_cr", True),
    "TradeReceivablesCurrent": ("trade_receivables_cr", True),
    "TradePayablesCurrent": ("trade_payables_cr", True),
    "Inventories": ("inventory_cr", True),
    "CashAndCashEquivalents": ("cash_equivalents_cr", True),
    "PropertyPlantAndEquipment": ("fixed_assets_cr", True),
    "CapitalWorkInProgress": ("cwip_cr", True),
    "Goodwill": ("goodwill_cr", True),
    "OtherIntangibleAssets": ("intangible_assets_cr", True),
    "CurrentInvestments": ("current_investments_cr", True),
    "NoncurrentInvestments": ("non_current_investments_cr", True),
    "DeferredTaxAssetsNet": ("deferred_tax_assets_cr", True),
    "OtherCurrentAssets": ("other_current_assets_cr_detail", True),
    "OtherNoncurrentAssets": ("other_non_current_assets_cr", True),
    "ProvisionsCurrent": ("provisions_current_cr", True),
    "ProvisionsNoncurrent": ("provisions_non_current_cr", True),
}

ANNUAL_CF_TAG_MAP = {
    "CashFlowsFromUsedInOperatingActivities": ("operating_cashflow_cr", True),
    "CashFlowsFromUsedInInvestingActivities": ("investing_cashflow_cr", True),
    "CashFlowsFromUsedInFinancingActivities": ("financing_cashflow_cr", True),
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": ("capex_cr", True),
    "DividendsPaidClassifiedAsFinancingActivities": ("dividend_per_share", True),
    "IncreaseDecreaseInCashAndCashEquivalents": ("net_cashflow_cr", True),
}

# ═══════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════

LOG_DIR = os.path.expanduser("~/logs")
os.makedirs(LOG_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f"pipeline_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("pipeline")


# ═══════════════════════════════════════════════════════════════════
# NSE SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

class NSESession:
    def __init__(self):
        self.session = None
        self.request_count = 0
        self.last_request_time = 0
        self._create_session()

    def _create_session(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.get("https://www.nseindia.com", timeout=15)
                if r.status_code == 200:
                    self.request_count = 0
                    log.info("NSE session created")
                    return
            except Exception as e:
                log.warning(f"NSE session init attempt {attempt+1}: {e}")
                time.sleep(2)
        log.warning("NSE session init failed — will retry on first request")

    def refresh(self):
        log.info("Refreshing NSE session...")
        self._create_session()
        time.sleep(1)

    def get(self, url, **kwargs):
        elapsed = time.time() - self.last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)

        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.get(url, timeout=15, **kwargs)
                self.last_request_time = time.time()
                self.request_count += 1

                if r.status_code == 200:
                    return r
                elif r.status_code == 403:
                    log.warning(f"403 — refreshing session (attempt {attempt+1})")
                    self.refresh()
                    time.sleep(2)
                elif r.status_code == 429:
                    wait = 5 * (attempt + 1)
                    log.warning(f"429 — waiting {wait}s")
                    time.sleep(wait)
                else:
                    return r
            except requests.exceptions.Timeout:
                log.warning(f"Timeout (attempt {attempt+1})")
                time.sleep(2)
            except Exception as e:
                log.warning(f"Request error: {e} (attempt {attempt+1})")
                time.sleep(2)
        return None


# ═══════════════════════════════════════════════════════════════════
# XBRL PARSING
# ═══════════════════════════════════════════════════════════════════

def parse_xbrl_quarterly(xbrl_url, nse):
    """Parse quarterly XBRL — extracts OneD (current quarter) values."""
    r = nse.get(xbrl_url)
    if not r or r.status_code != 200:
        return None, None, None

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log.warning(f"XML parse error: {e}")
        return None, None, None

    vals = {}
    raw = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if not elem.text or not elem.text.strip():
            continue

        # Match OneD context for current quarter data
        # Exclude segment-specific contexts (handled separately)
        if "Reportable" in ctx:
            continue
        if ctx == "OneD" or (ctx.endswith("1D") and len(ctx) <= 6):
            val = elem.text.strip()
            if tag not in vals:
                vals[tag] = val
                raw[tag] = val

    is_banking = vals.get("ResultType", "").strip() == "Banking Format" or "NameOfBank" in vals or "InterestEarned" in vals
    return vals, root, raw, is_banking


def parse_xbrl_annual(xbrl_url, nse):
    """Parse annual XBRL — extracts ALL values (first occurrence)."""
    r = nse.get(xbrl_url)
    if not r or r.status_code != 200:
        return None, None

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log.warning(f"XML parse error: {e}")
        return None, None

    vals = {}
    raw = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if not elem.text or not elem.text.strip():
            continue
        # Skip segment contexts
        if "Reportable" in ctx:
            continue
        val = elem.text.strip()
        if tag not in vals:
            vals[tag] = val
            raw[tag] = val

    return vals, raw


def extract_segments(root):
    """
    Extract segment data from XBRL. Context patterns:
      Revenue:      OneReportable{N}D  (N=segment number 1-6)
      Profit:       OneReportableFinance{N}D
      Assets:       OneReportable3{N}I
      Liabilities:  OneReportable4{N}I
      Cumulative:   FourReportable... (skip)
    """
    if root is None:
        return []

    segments = {}  # seg_num -> {name, revenue, profit, assets, liabilities}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if not elem.text or not elem.text.strip():
            continue

        if "Reportable" not in ctx:
            continue

        # Skip cumulative contexts (Four = 4 quarters)
        if ctx.startswith("Four"):
            continue

        val = elem.text.strip()

        # NEW FORMAT Revenue: OneReportable{N}D (e.g., OneReportable1D)
        # OLD FORMAT Revenue: OneReportableSegmentRevenue{NN}D (e.g., OneReportableSegmentRevenue01D)
        m = re.match(r"OneReportable(?:SegmentRevenue)?(\d+)D$", ctx)
        if m:
            seg_num = m.group(1)
            if seg_num not in segments:
                segments[seg_num] = {}
            if tag == "DescriptionOfReportableSegment":
                segments[seg_num]["name"] = val
            elif tag == "SegmentRevenue":
                if "revenue" not in segments[seg_num]:
                    try:
                        segments[seg_num]["revenue"] = float(val) / INR_TO_CR
                    except (ValueError, TypeError):
                        pass
            continue

        # NEW FORMAT Profit: OneReportableFinance{N}D
        # OLD FORMAT Profit: OneReportableSegmentResults{NN}D
        m = re.match(r"OneReportable(?:Finance|SegmentResults)(\d+)D$", ctx)
        if m:
            seg_num = m.group(1)
            if seg_num not in segments:
                segments[seg_num] = {}
            if tag == "DescriptionOfReportableSegment":
                segments[seg_num]["name"] = val
            elif tag == "SegmentProfitLossBeforeTaxAndFinanceCosts":
                if "profit" not in segments[seg_num]:
                    try:
                        segments[seg_num]["profit"] = float(val) / INR_TO_CR
                    except (ValueError, TypeError):
                        pass
            continue

        # NEW FORMAT Assets: OneReportable3{N}I / OneReportable3{N}D
        # OLD FORMAT Assets: OneReportableSegmentAssets{NN}D / OneReportableSegmentAssets{NN}I
        m = re.match(r"OneReportable(?:3|SegmentAssets)(\d+)[DI]$", ctx)
        if m:
            seg_num = m.group(1)
            if seg_num not in segments:
                segments[seg_num] = {}
            if tag == "DescriptionOfReportableSegment":
                segments[seg_num]["name"] = val
            elif tag == "SegmentAssets":
                if "assets" not in segments[seg_num]:
                    try:
                        segments[seg_num]["assets"] = float(val) / INR_TO_CR
                    except (ValueError, TypeError):
                        pass
            continue

        # NEW FORMAT Liabilities: OneReportable4{N}I / OneReportable4{N}D
        # OLD FORMAT Liabilities: OneReportableSegmentLiabilities{NN}D / {NN}I
        m = re.match(r"OneReportable(?:4|SegmentLiabilities)(\d+)[DI]$", ctx)
        if m:
            seg_num = m.group(1)
            if seg_num not in segments:
                segments[seg_num] = {}
            if tag == "DescriptionOfReportableSegment":
                segments[seg_num]["name"] = val
            elif tag == "SegmentLiabilities":
                if "liabilities" not in segments[seg_num]:
                    try:
                        segments[seg_num]["liabilities"] = float(val) / INR_TO_CR
                    except (ValueError, TypeError):
                        pass
            continue

        # OLD FORMAT FALLBACK: OneReportableSegmentRevenue01D pattern
        # Group by matching the segment description sequentially
        if ctx.startswith("One") and "Reportable" in ctx and ctx.endswith("1D"):
            seg_key = "old_" + str(len(segments))
            if tag == "DescriptionOfReportableSegment":
                # Check if this name already exists under a different key
                existing = False
                for k, v in segments.items():
                    if v.get("name") == val:
                        existing = True
                        break
                if not existing:
                    if seg_key not in segments:
                        segments[seg_key] = {}
                    segments[seg_key]["name"] = val

    # Convert to sorted list
    result = []
    for seg_num in sorted(segments.keys(), key=lambda x: int(x) if x.isdigit() else 99):
        data = segments[seg_num]
        if data.get("name") and (data.get("revenue") or data.get("profit")):
            result.append({
                "segment_name": data["name"],
                "segment_revenue_cr": data.get("revenue"),
                "segment_profit_cr": data.get("profit"),
                "segment_assets_cr": data.get("assets"),
                "segment_liabilities_cr": data.get("liabilities"),
            })

    return result


def xbrl_to_cr(val_str):
    try:
        return float(val_str) / INR_TO_CR
    except (ValueError, TypeError):
        return None


def xbrl_to_float(val_str):
    try:
        return float(val_str)
    except (ValueError, TypeError):
        return None


def map_xbrl_to_record(vals, tag_map):
    record = {}
    for xbrl_tag, (db_col, is_monetary) in tag_map.items():
        if xbrl_tag in vals:
            if is_monetary:
                record[db_col] = xbrl_to_cr(vals[xbrl_tag])
            else:
                record[db_col] = xbrl_to_float(vals[xbrl_tag])
    return record


def compute_operating_profit(record):
    """Compute operating profit and OPM from P&L line items."""
    rev = record.get("revenue_cr", 0) or 0
    rm = record.get("raw_material_cost_cr", 0) or 0
    pst = record.get("purchases_stock_in_trade_cr", 0) or 0
    cii = record.get("changes_in_inventories_cr", 0) or 0
    emp = record.get("employee_cost_cr", 0) or 0
    oe = record.get("other_expenses_cr", 0) or 0

    operating_expenses = rm + pst + cii + emp + oe
    operating_profit = rev - operating_expenses if rev else None
    opm = (operating_profit / rev * 100) if rev and operating_profit is not None else None

    record["operating_profit_cr"] = operating_profit
    record["opm_percent"] = round(opm, 2) if opm is not None else None


# ═══════════════════════════════════════════════════════════════════
# NSE DATA FETCHERS
# ═══════════════════════════════════════════════════════════════════

def fetch_quarterly_filings(symbol, nse, consolidated_type="Consolidated"):
    all_filings = []

    for from_d, to_d in QUARTERLY_RANGES:
        r = nse.get(
            f"https://www.nseindia.com/api/corporates-financial-results"
            f"?index=equities&period=Quarterly&from_date={from_d}&to_date={to_d}"
        )
        if r and r.status_code == 200:
            try:
                data = r.json()
                matched = [
                    d for d in data
                    if d.get("symbol") == symbol
                    and d.get("consolidated") == consolidated_type
                    and d.get("xbrl")
                ]
                all_filings.extend(matched)
            except Exception:
                pass
        time.sleep(REQUEST_DELAY)

    # Integrated filing quarters (2025+)
    r = nse.get(f"https://www.nseindia.com/api/integrated-filing-results?symbol={symbol}")
    if r and r.status_code == 200:
        try:
            data = r.json().get("data", [])
            consol = [
                d for d in data
                if "Financials" in d.get("type", "")
                and d.get("consolidated") == consolidated_type
                and d.get("xbrl")
            ]
            all_filings.extend(consol)
        except Exception:
            pass

    # Deduplicate by XBRL URL
    seen = set()
    unique = []
    for f in all_filings:
        url = f.get("xbrl", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(f)

    return unique


def fetch_annual_filings(symbol, nse, consolidated_type="Consolidated"):
    all_filings = []

    for from_d, to_d in ANNUAL_RANGES:
        r = nse.get(
            f"https://www.nseindia.com/api/corporates-financial-results"
            f"?index=equities&period=Annual&from_date={from_d}&to_date={to_d}"
        )
        if r and r.status_code == 200:
            try:
                data = r.json()
                matched = [
                    d for d in data
                    if d.get("symbol") == symbol
                    and d.get("consolidated") == consolidated_type
                    and d.get("xbrl")
                ]
                all_filings.extend(matched)
            except Exception:
                pass
        time.sleep(REQUEST_DELAY)

    seen = set()
    unique = []
    for f in all_filings:
        url = f.get("xbrl", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(f)

    return unique


def fetch_shareholding(symbol, nse):
    r = nse.get(
        f"https://www.nseindia.com/api/corporate-share-holdings-master"
        f"?index=equities&symbol={symbol}"
    )
    if r and r.status_code == 200:
        try:
            data = r.json()
            return data[:20] if isinstance(data, list) else []
        except Exception:
            pass
    return []


# ═══════════════════════════════════════════════════════════════════
# SUPABASE WRITERS
# ═══════════════════════════════════════════════════════════════════

def store_records(table_name, records, on_conflict, supabase, batch_size=20):
    if not records:
        return 0

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        try:
            supabase.table(table_name).upsert(batch, on_conflict=on_conflict).execute()
        except Exception as e:
            log.error(f"{table_name} write error (batch {i}): {str(e)[:200]}")
        time.sleep(0.2)

    return len(records)


# ═══════════════════════════════════════════════════════════════════
# PROCESS SINGLE STOCK
# ═══════════════════════════════════════════════════════════════════

def process_stock(symbol, security_id, nse, supabase):
    stats = {"quarterly": 0, "annual": 0, "segments": 0, "shareholding": 0}

    # ─── QUARTERLY P&L + SEGMENTS ───
    # Try consolidated first, fall back to standalone
    filings = fetch_quarterly_filings(symbol, nse, "Consolidated")
    is_consolidated = True
    if not filings:
        filings = fetch_quarterly_filings(symbol, nse, "Non-Consolidated")
        is_consolidated = False
        if filings:
            log.info(f"     ↳ {symbol}: using Standalone results (no Consolidated)")

    quarterly_records = []
    segment_records = []

    for filing in filings:
        xbrl_url = filing.get("xbrl", "")
        if not xbrl_url:
            continue

        parse_result = parse_xbrl_quarterly(xbrl_url, nse)
        if not parse_result or not parse_result[0]:
            continue
        if len(parse_result) == 4:
            vals, root, raw, is_banking = parse_result
        else:
            vals, root, raw = parse_result
            is_banking = vals.get("ResultType", "").strip() == "Banking Format" or "NameOfBank" in vals or "InterestEarned" in vals

        # Determine period dates
        from_date = filing.get("fromDate") or vals.get("DateOfStartOfReportingPeriod")
        to_date = filing.get("toDate") or vals.get("DateOfEndOfReportingPeriod")
        qe_date = filing.get("qe_Date")

        period_end = None
        for src in [to_date, qe_date]:
            if not src:
                continue
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%b-%y"):
                try:
                    period_end = datetime.strptime(src, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            if period_end:
                break

        if not period_end:
            continue

        # Map XBRL to database columns
        if is_banking:
            record = {}
            for xbrl_tag, (db_col, _) in BANKING_TAG_MAP.items():
                if xbrl_tag in vals:
                    try:
                        record[db_col] = float(vals[xbrl_tag]) / 10000000
                    except (ValueError, TypeError):
                        pass
            # EPS and ratios — no division
            for tag, col in [("BasicEarningsPerShareBeforeExtraordinaryItems", "eps"),
                             ("DilutedEarningsPerShareBeforeExtraordinaryItems", "eps_diluted"),
                             ("FaceValueOfEquityShareCapital", "face_value")]:
                if tag in vals:
                    try:
                        record[col] = float(vals[tag])
                    except:
                        pass
            rev = record.get("revenue_cr", 0) or 0
            op = record.get("operating_profit_cr", 0) or 0
            record["opm_percent"] = round(op / rev * 100, 2) if rev else None
        else:
            record = map_xbrl_to_record(vals, QUARTERLY_TAG_MAP)
            compute_operating_profit(record)

        # Metadata
        record["security_id"] = security_id
        record["symbol"] = symbol
        record["period_end_date"] = period_end
        record["period"] = "Quarterly"
        record["is_consolidated"] = is_consolidated
        record["is_audited"] = filing.get("audited", "") == "Audited"
        record["has_exceptional_items"] = bool(record.get("exceptional_items_cr"))
        record["nse_symbol"] = symbol
        record["nse_from_date"] = from_date
        record["nse_to_date"] = to_date
        record["xbrl_source_url"] = xbrl_url
        record["filing_date"] = filing.get("broadcastDate", filing.get("broadcastDt"))
        record["raw_xbrl_data"] = json.dumps(raw) if raw else None

        quarterly_records.append(record)

        # Extract segments
        segments = extract_segments(root)
        if segments:
            total_rev = sum(s.get("segment_revenue_cr", 0) or 0 for s in segments)
            for i, seg in enumerate(segments):
                seg_rev = seg.get("segment_revenue_cr", 0) or 0
                seg_prof = seg.get("segment_profit_cr", 0) or 0

                segment_records.append({
                    "security_id": security_id,
                    "symbol": symbol,
                    "period_end_date": period_end,
                    "is_consolidated": is_consolidated,
                    "segment_name": seg["segment_name"],
                    "segment_order": i + 1,
                    "segment_revenue_cr": seg_rev if seg_rev else None,
                    "segment_profit_cr": seg_prof if seg_prof else None,
                    "segment_assets_cr": seg.get("segment_assets_cr"),
                    "segment_liabilities_cr": seg.get("segment_liabilities_cr"),
                    "segment_revenue_pct": round(seg_rev / total_rev * 100, 2) if total_rev else None,
                    "segment_margin_pct": round(seg_prof / seg_rev * 100, 2) if seg_rev else None,
                    "xbrl_source_url": xbrl_url,
                })

        time.sleep(REQUEST_DELAY)

    stats["quarterly"] = store_records(
        "financials_quarterly", quarterly_records,
        "symbol,period_end_date,is_consolidated", supabase
    )
    # Deduplicate segments (same segment name can appear in multiple XBRL contexts)
    seen_seg = set()
    unique_seg = []
    for seg in segment_records:
        key = (seg['period_end_date'], seg['segment_name'])
        if key not in seen_seg:
            seen_seg.add(key)
            unique_seg.append(seg)
    segment_records = unique_seg

    stats["segments"] = store_records(
        "segments_quarterly", segment_records,
        "symbol,period_end_date,segment_name,is_consolidated", supabase
    )

    # ─── ANNUAL P&L + BS + CF ───
    annual_filings = fetch_annual_filings(symbol, nse, "Consolidated" if is_consolidated else "Non-Consolidated")
    annual_records = []

    for filing in annual_filings:
        xbrl_url = filing.get("xbrl", "")
        if not xbrl_url:
            continue

        vals, raw = parse_xbrl_annual(xbrl_url, nse)
        if not vals:
            continue
        
        is_banking_annual = vals.get("ResultType", "").strip() == "Banking Format" or "NameOfBank" in vals or "InterestEarned" in vals

        from_date = filing.get("fromDate") or vals.get("DateOfStartOfReportingPeriod")
        to_date = filing.get("toDate") or vals.get("DateOfEndOfReportingPeriod")

        period_end = None
        if to_date:
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    period_end = datetime.strptime(to_date, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
        if not period_end:
            continue

        # Fiscal year
        try:
            end_dt = datetime.strptime(period_end, "%Y-%m-%d")
            if end_dt.month <= 3:
                fiscal_year = f"FY{end_dt.year-1}-{str(end_dt.year)[-2:]}"
            else:
                fiscal_year = f"FY{end_dt.year}-{str(end_dt.year+1)[-2:]}"
        except:
            fiscal_year = None

        # Map P&L
        if is_banking_annual:
            record = {}
            for xbrl_tag, (db_col, _) in BANKING_TAG_MAP.items():
                if xbrl_tag in vals:
                    try:
                        record[db_col] = float(vals[xbrl_tag])
                    except (ValueError, TypeError):
                        pass
            rev = record.get("revenue_cr", 0) or 0
            op = record.get("operating_profit_cr", 0) or 0
            record["opm_percent"] = round(op / rev * 100, 2) if rev else None
        else:
            record = map_xbrl_to_record(vals, QUARTERLY_TAG_MAP)

            # Map Balance Sheet
            bs_data = map_xbrl_to_record(vals, ANNUAL_BS_TAG_MAP)
            record.update(bs_data)

            # Map Cash Flow
            cf_data = map_xbrl_to_record(vals, ANNUAL_CF_TAG_MAP)
            record.update(cf_data)

            # Compute derived
            compute_operating_profit(record)

        lt_borr = record.get("long_term_borrowings_cr") or 0
        st_borr = record.get("short_term_borrowings_cr") or 0
        record["total_borrowings_cr"] = (lt_borr + st_borr) or None

        ci = record.get("current_investments_cr") or 0
        nci = record.get("non_current_investments_cr") or 0
        record["investments_cr"] = (ci + nci) or None

        ocf = record.get("operating_cashflow_cr") or 0
        capex = record.get("capex_cr") or 0
        record["free_cashflow_cr"] = (ocf - capex) if (ocf or capex) else None

        equity = record.get("total_equity_cr") or 0
        net_profit = record.get("net_profit_cr") or 0
        curr_assets = record.get("total_current_assets_cr") or 0
        curr_liab = record.get("total_current_liabilities_cr") or 0
        interest = record.get("interest_cr") or 0
        pbt = record.get("profit_before_tax_cr") or 0

        record["roe"] = round(net_profit / equity * 100, 2) if equity else None
        record["current_ratio"] = round(curr_assets / curr_liab, 2) if curr_liab else None
        record["interest_coverage"] = round((pbt + interest) / interest, 2) if interest else None
        total_borr = record.get("total_borrowings_cr") or 0
        if total_borr and equity:
            record["debt_to_equity"] = round(total_borr / equity, 2)

        # Metadata
        record["security_id"] = security_id
        record["symbol"] = symbol
        record["period_end_date"] = period_end
        record["fiscal_year"] = fiscal_year
        record["is_consolidated"] = is_consolidated
        record["is_audited"] = True
        record["nse_symbol"] = symbol
        record["nse_from_date"] = from_date
        record["nse_to_date"] = to_date
        record["xbrl_source_url"] = xbrl_url
        record["raw_xbrl_data"] = json.dumps(raw) if raw else None

        annual_records.append(record)
        time.sleep(REQUEST_DELAY)

    stats["annual"] = store_records(
        "financials_annual", annual_records,
        "symbol,period_end_date,is_consolidated", supabase
    )

    # ─── SHAREHOLDING ───
    shp_data = fetch_shareholding(symbol, nse)
    shp_records = []

    for entry in shp_data:
        date_str = entry.get("date", "")
        period_end = None
        for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
            try:
                period_end = datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if not period_end:
            continue

        promoter = entry.get("pr_and_prgrp")
        public = entry.get("public_val")
        if promoter == 0 and public == 0:
            continue

        shp_records.append({
            "security_id": security_id,
            "symbol": symbol,
            "period_end_date": period_end,
            "period": "Quarterly",
            "promoter_percent": float(promoter) if promoter else None,
            "public_percent": float(public) if public else None,
            "employee_trusts_percent": float(entry.get("employeeTrusts", 0)) if entry.get("employeeTrusts") else None,
            "xbrl_source_url": entry.get("xbrl"),
            "source_url": entry.get("xbrl"),
            "submission_date": entry.get("submissionDate"),
        })

    stats["shareholding"] = store_records(
        "shareholding_quarterly", shp_records,
        "symbol,period_end_date", supabase
    )

    return stats


# ═══════════════════════════════════════════════════════════════════
# RESUME CHECK
# ═══════════════════════════════════════════════════════════════════

def is_stock_processed(symbol, supabase):
    try:
        r = (
            supabase.table("financials_quarterly")
            .select("symbol", count="exact")
            .eq("symbol", symbol)
            .limit(1)
            .execute()
        )
        return r.count and r.count > 0
    except:
        return False


# ═══════════════════════════════════════════════════════════════════
# LOAD UNIVERSE
# ═══════════════════════════════════════════════════════════════════

def load_universe(supabase, specific_symbols=None):
    all_stocks = []
    offset = 0

    while True:
        query = (
            supabase.table("stock_universe")
            .select("security_id,symbol")
            .eq("is_active", True)
            .eq("is_etf", False)
        )

        if specific_symbols:
            query = query.in_("symbol", specific_symbols)

        result = query.range(offset, offset + 999).execute()
        if not result.data:
            break
        all_stocks.extend(result.data)
        if len(result.data) < 1000:
            break
        offset += 1000

    return all_stocks


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(IST)
    specific_symbols = sys.argv[1:] if len(sys.argv) > 1 else None

    log.info("=" * 70)
    log.info("  VALVO Intelligence — NSE Fundamentals Pipeline")
    log.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    if specific_symbols:
        log.info(f"  Mode: Specific stocks — {', '.join(specific_symbols)}")
    else:
        log.info("  Mode: Full universe backfill")
    log.info("=" * 70)

    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info("Supabase connected")

    stocks = load_universe(supabase, specific_symbols)
    if not stocks:
        log.error("No stocks found")
        sys.exit(1)

    total = len(stocks)
    log.info(f"Stocks to process: {total}")

    nse = NSESession()

    processed = 0
    skipped = 0
    failed = 0
    total_q = 0
    total_a = 0
    total_s = 0
    total_sh = 0
    start_time = time.time()
    failed_symbols = []

    for i, stock in enumerate(stocks):
        symbol = stock["symbol"]
        security_id = stock["security_id"]

        # Resume: skip if already has data
        if not specific_symbols and is_stock_processed(symbol, supabase):
            skipped += 1
            if skipped <= 10 or skipped % 100 == 0:
                log.info(f"  [{i+1}/{total}] {symbol}: skipped (already processed)")
            continue

        # Refresh session periodically
        if nse.request_count > 0 and nse.request_count % (SESSION_REFRESH_EVERY * 10) == 0:
            nse.refresh()

        # Process
        try:
            stats = process_stock(symbol, security_id, nse, supabase)

            total_q += stats["quarterly"]
            total_a += stats["annual"]
            total_s += stats["segments"]
            total_sh += stats["shareholding"]

            if sum(stats.values()) > 0:
                processed += 1
                log.info(
                    f"  ✅ [{i+1}/{total}] {symbol}: "
                    f"Q={stats['quarterly']} A={stats['annual']} "
                    f"S={stats['segments']} SH={stats['shareholding']}"
                )
            else:
                failed += 1
                failed_symbols.append(symbol)
                log.warning(f"  ⏭️ [{i+1}/{total}] {symbol}: no data")

        except Exception as e:
            failed += 1
            failed_symbols.append(symbol)
            log.error(f"  ❌ [{i+1}/{total}] {symbol}: {str(e)[:100]}")

        # Progress every 25 stocks
        if (i + 1) % 25 == 0 or (i + 1) == total:
            elapsed = time.time() - start_time
            active = i + 1 - skipped
            rate = active / elapsed if elapsed > 0 else 0
            remaining = (total - i - 1) / rate / 60 if rate > 0 else 0

            log.info(
                f"\n  📊 PROGRESS: {i+1}/{total} | "
                f"✅ {processed} | ⏭️ {skipped} skipped | ❌ {failed}\n"
                f"     Q: {total_q} | A: {total_a} | "
                f"S: {total_s} | SH: {total_sh}\n"
                f"     Elapsed: {elapsed/60:.1f}m | "
                f"~{remaining:.0f}m remaining | "
                f"Requests: {nse.request_count}\n"
            )

    elapsed_total = time.time() - start_time

    log.info("=" * 70)
    log.info("  PIPELINE COMPLETE")
    log.info("=" * 70)
    log.info(f"  Stocks processed   : {processed}")
    log.info(f"  Stocks skipped     : {skipped}")
    log.info(f"  Stocks failed      : {failed}")
    log.info(f"  Quarterly records  : {total_q}")
    log.info(f"  Annual records     : {total_a}")
    log.info(f"  Segment records    : {total_s}")
    log.info(f"  Shareholding recs  : {total_sh}")
    log.info(f"  NSE requests       : {nse.request_count}")
    log.info(f"  Time               : {elapsed_total/60:.1f} minutes")
    log.info(f"  Log                : {log_file}")
    log.info("=" * 70)

    if failed_symbols:
        log.info(f"\n  Failed ({len(failed_symbols)}):")
        for c in range(0, len(failed_symbols), 20):
            log.info(f"    {', '.join(failed_symbols[c:c+20])}")


if __name__ == "__main__":
    main()
