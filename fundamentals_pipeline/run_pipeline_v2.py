"""
VALVO Intelligence — NSE Fundamentals Pipeline v2
===================================================
Fresh-run pipeline for v2 tables with multi-format XBRL support:
  1. Quarterly P&L (FY20 → FY26) from XBRL — 3 formats supported
  2. Segment breakdown from XBRL
  3. Annual P&L + Balance Sheet + Cash Flow from XBRL
  4. Shareholding pattern from NSE API

XBRL formats auto-detected per filing:
  - Corporate: standard IndAS (RevenueFromOperations, OtherIncome...)
  - Banking: NII-based (InterestEarned, InterestExpended, Provisions...)
  - Insurance: Premium-based (GrossPremiumIncome, BenefitsPaidNet...)
  - NBFCs: Use corporate format (natural fallthrough)

Target: financials_quarterly_v2, financials_annual_v2, segments_quarterly_v2,
        shareholding_quarterly_v2

Initial run: ~20 hours for full universe (all ~2,127 active equities)
Every failure logs to pipeline_failures table for later analysis.

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

# ═══ V2 TABLE NAMES (fresh run target) ═══
TABLE_QUARTERLY = "financials_quarterly"
TABLE_ANNUAL = "financials_annual"
TABLE_SEGMENTS = "segments_quarterly"
TABLE_SHAREHOLDING = "shareholding_quarterly"
TABLE_FAILURES = "pipeline_failures"

# NSE API rate limiting
REQUEST_DELAY = 0.8
SESSION_REFRESH_EVERY = 50
MAX_RETRIES = 4

# Run ID for this pipeline invocation (for filtering failures later)
RUN_ID = datetime.now(IST).strftime("%Y%m%d_%H%M%S")

# Date ranges for historical data (FY20 to FY26 — extended to catch latest)
QUARTERLY_RANGES = [
    ("01-04-2019", "31-03-2020"),
    ("01-04-2020", "31-03-2021"),
    ("01-04-2021", "31-03-2022"),
    ("01-04-2022", "31-03-2023"),
    ("01-04-2023", "31-03-2024"),
    ("01-04-2024", "31-03-2025"),
    ("01-04-2025", "31-03-2026"),  # NEW: captures Q1-Q4 FY26
]

ANNUAL_RANGES = [
    ("01-04-2019", "31-03-2020"),
    ("01-04-2020", "31-03-2021"),
    ("01-04-2021", "31-03-2022"),
    ("01-04-2022", "31-03-2023"),
    ("01-04-2023", "31-03-2024"),
    ("01-04-2024", "31-03-2025"),
    ("01-04-2025", "31-03-2026"),  # NEW: captures latest FY25 annual filings
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
# SEGMENT NAME CANONICALIZATION
# ═══════════════════════════════════════════════════════════════════
#
# NSE XBRL filings inconsistently name segments across quarters due to
# source data quality (different people typing, copy-paste errors, case
# toggle mistakes, punctuation drift). Examples from real data:
#   "Home Care" / "Home care" / "HOME CARE"                 — casing
#   "Pellet Plant" / "Pellet plant" / "PelletPlant"          — case + spacing
#   "Beauty & Personal Care" / "Beauty and Personal care"    — & vs and
#   "Others (includes Exports)" / "Others (includes exports, Infant & Feminine Care etc.)"
#                                                            — qualifier drift
#   "Foods" / "Food & Refreshment" / "Beauty & Wellbeing"   — GENUINE restructurings
#
# We store the RAW name as `segment_name` for display, and a CANONICAL KEY
# as `segment_canonical_key` for grouping/deduplication.
#
# Canonicalization rules (conservative — avoids over-merging):
#   1. camelCase/PascalCase → insert spaces (PelletPlant → Pellet Plant)
#   2. Lowercase
#   3. Replace & with 'and'
#   4. Strip all non-alphanumeric (removes punctuation, brackets)
#   5. Collapse whitespace, tokenize
#   6. Remove English stop-words
#   7. Apply light stemming: strip -s, -es, -ing from tokens >4 chars
#   8. Sort tokens, join with single space
#
# This catches casing, spacing, punctuation, & vs 'and', ordering, and
# singular/plural variations. It does NOT catch typos ("Beaty" vs "Beauty")
# — those require fuzzy matching which is a scoring-layer concern.

_CAMEL_SPLIT_RE = re.compile(r'([a-z])([A-Z])')
_NON_ALPHANUM_RE = re.compile(r'[^a-z0-9\s]')
_WHITESPACE_RE = re.compile(r'\s+')

_SEGMENT_STOPWORDS = frozenset({
    # English fillers
    'the', 'of', 'and', 'for', 'a', 'an', 'to', 'in', 'on', 'at', 'by', 'with',
    'from', 'as', 'or', 'is', 'are',
    # Filing conventions that add no semantic value
    'etc', 'incl', 'includes', 'including', 'net', 'expenses', 'expense',
    'others', 'other',
})


def _stem(token):
    """Light stemmer — handles common singular/plural & gerund forms.
    Only applies to tokens longer than 3 chars to avoid mangling short words."""
    if len(token) <= 3:
        return token
    # -ingredients -> ingredient; -refreshments -> refreshment
    if token.endswith('ments'):
        return token[:-1]  # ments -> ment
    if token.endswith('ings'):
        return token[:-1]  # ings -> ing
    # -ies -> y (companies -> company)
    if token.endswith('ies') and len(token) > 4:
        return token[:-3] + 'y'
    # -s -> (foods -> food, services -> service)
    # Safer: only strip trailing -s if not -ss (business -> business, not busines)
    if token.endswith('s') and not token.endswith('ss') and len(token) > 4:
        return token[:-1]
    return token


def canonicalize_segment_name(name):
    """Compute canonical key for a segment name. Returns None if input is None/empty.

    Used for grouping equivalent segment names across quarters.
    See SEGMENT NAME CANONICALIZATION section above for rules.

    Fallback: if all words are stop-words (e.g. "Others" alone), return the
    lowercased cleaned form so we don't lose the segment.
    """
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None

    # 1. Split camelCase/PascalCase (PelletPlant -> Pellet Plant)
    s = _CAMEL_SPLIT_RE.sub(r'\1 \2', s)

    # 2. Lowercase
    s = s.lower()

    # 3. Replace & with 'and'
    s = s.replace('&', ' and ')

    # 4. Strip punctuation/non-alphanumeric (keeps letters, digits, spaces)
    s = _NON_ALPHANUM_RE.sub(' ', s)

    # 5. Collapse whitespace, tokenize
    raw_tokens = _WHITESPACE_RE.sub(' ', s).strip().split()

    if not raw_tokens:
        return None

    # 6. Stop-words filter
    tokens = [t for t in raw_tokens if t not in _SEGMENT_STOPWORDS]

    # Fallback: if all tokens were stop-words (e.g. bare "Others" → empty),
    # keep original lowercased tokens so the segment isn't lost.
    if not tokens:
        tokens = raw_tokens

    # 7. Stemming
    tokens = [_stem(t) for t in tokens]

    # 8. Sort + join
    return ' '.join(sorted(tokens))


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
    # Fallback: some corporates use 'ProfitLossForThePeriod' instead (e.g. LT)
    "ProfitLossForThePeriod": ("net_profit_cr", True),
    "ProfitOrLossAttributableToOwnersOfParent": ("adjusted_net_profit_cr", True),
    "ProfitOrLossAttributableToNonControllingInterests": ("minority_interest_cr", True),
    "OtherComprehensiveIncomeNetOfTaxes": ("other_comprehensive_income_cr", True),
    "ComprehensiveIncomeForThePeriod": ("comprehensive_income_cr", True),
    "PaidUpValueOfEquityShareCapital": ("paid_up_capital_cr", True),
    "FaceValueOfEquityShareCapital": ("face_value", False),
    "BasicEarningsLossPerShareFromContinuingOperations": ("eps", False),
    "DilutedEarningsLossPerShareFromContinuingOperations": ("eps_diluted", False),
    # Fallback EPS tag names used by some corporates
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": ("eps", False),
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": ("eps_diluted", False),
    "DebtEquityRatio": ("debt_equity_ratio", False),
    "DebtServiceCoverageRatio": ("debt_service_coverage_ratio", False),
    "InterestServiceCoverageRatio": ("interest_service_coverage_ratio", False),
}

# ─── BANKING TAG MAP ─────────────────────────────────────────────────
# Used for banks where XBRL uses InterestEarned/InterestExpended instead of
# RevenueFromOperations. Detection: presence of 'InterestEarned' tag.
BANKING_QUARTERLY_TAG_MAP = {
    # Income
    "InterestEarned": ("revenue_cr", True),                # Core bank income
    "OtherIncome": ("other_income_cr", True),
    "Income": ("total_income_cr", True),

    # Expenses
    "InterestExpended": ("interest_cr", True),             # Interest paid on deposits
    "OperatingExpenses": ("other_expenses_cr", True),
    "ExpenditureExcludingProvisionsAndContingencies": ("expenses_cr", True),
    "EmployeesRemunerationAndWelfareExpenses": ("employee_cost_cr", True),

    # Profit — direct from XBRL (don't compute for banks)
    "OperatingProfitBeforeProvisionAndContingencies": ("operating_profit_cr", True),
    "ProvisionsOtherThanTaxAndContingencies": ("provisions_cr", True),  # Banking-specific
    "ProfitLossFromOrdinaryActivitiesBeforeTax": ("profit_before_tax_cr", True),
    "ProfitLossFromOrdinaryActivitiesAfterTax": ("net_profit_cr", True),
    "ProfitLossForThePeriod": ("net_profit_cr", True),     # Fallback
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": ("adjusted_net_profit_cr", True),
    "ProfitLossOfMinorityInterest": ("minority_interest_cr", True),

    # Tax
    "TaxExpense": ("tax_cr", True),

    # EPS — different tag names than corporates
    "BasicEarningsPerShareAfterExtraordinaryItems": ("eps", False),
    "DilutedEarningsPerShareAfterExtraordinaryItems": ("eps_diluted", False),
}

# ─── INSURANCE TAG MAP ────────────────────────────────────────────────
# Used for life/general insurance companies. Detection: presence of
# 'GrossPremiumIncome' or 'NetPremiumIncome' tag.
INSURANCE_QUARTERLY_TAG_MAP = {
    # Income
    "GrossPremiumIncome": ("revenue_cr", True),            # Top-line premium
    "NetPremiumIncome": ("net_premium_cr", True),          # Net of reinsurance
    "IncomeFromInvestmentsNet": ("other_income_cr", True), # Investment income
    "Income": ("total_income_cr", True),

    # Expenses
    "BenefitsPaidNet": ("benefits_paid_cr", True),         # Claims paid (biggest expense)
    "Commission": ("commission_cr", True),                 # Agent commission
    "OperatingExpensesRelatedToInsuranceBusiness": ("other_expenses_cr", True),
    "EmployeesRemunerationAndWelfareExpenses": ("employee_cost_cr", True),
    "Expenses": ("expenses_cr", True),

    # Profit — direct from XBRL
    "ProfitLossBeforeTax": ("profit_before_tax_cr", True),
    "ProfitLossAfterTaxBeforeExtraordinaryItems": ("net_profit_cr", True),
    "ProfitLossAfterTaxAndExtraordinaryItems": ("adjusted_net_profit_cr", True),
    "SurplusShownInTheRevenueAccount": ("surplus_cr", True),  # Key insurance metric

    # Tax
    "ProvisionForTax": ("tax_cr", True),

    # EPS — note the absurd long tag name
    "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized": ("eps", False),
    "BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized": ("eps_diluted", False),
}

# ─── GENERAL INSURANCE TAG MAP ────────────────────────────────────────
# Used for general (non-life) insurance companies like NIACL, ICICIGI,
# SBIG, etc. Detection: presence of 'GrossPremiumsWritten' tag.
# Key difference from life insurance: premium is WRITTEN (not earned),
# claims are measured incurred vs paid, underwriting profit/loss is
# the key metric.
INSURANCE_GENERAL_QUARTERLY_TAG_MAP = {
    # Income
    "GrossPremiumsWritten": ("revenue_cr", True),          # Top-line — premium written
    "NetPremiumWritten": ("net_premium_cr", True),         # Net of reinsurance ceded
    "PremiumEarned": ("total_income_cr", True),            # Earned in period
    "IncomeFromInvestmentsNet": ("other_income_cr", True), # Investment income
    "OperatingIncome": ("total_income_cr", True),          # Fallback for total income

    # Expenses
    "IncurredClaims": ("benefits_paid_cr", True),          # Claims incurred (key P&L item)
    "ClaimsPaid": ("benefits_paid_cr", True),              # Fallback if IncurredClaims absent
    "NetCommission": ("commission_cr", True),
    "CommissionsAndBrokerageNet": ("commission_cr", True),  # Fallback
    "OperatingExpensesRelatedToInsuranceBusiness": ("other_expenses_cr", True),
    "EmployeesRemunerationAndWelfareExpenses": ("employee_cost_cr", True),
    "OperatingExpenses": ("expenses_cr", True),

    # Profit — direct from XBRL
    "UnderwritingProfitOrLoss": ("surplus_cr", True),      # Reuse insurance-specific col
    "OperatingProfitOrLoss": ("operating_profit_cr", True),
    "ProfitOrLossBeforeExtraordinaryItems": ("pbt_before_exceptional_cr", True),
    "ProfitOrLossBeforeTax": ("profit_before_tax_cr", True),
    "ProfitLossAfterTax": ("net_profit_cr", True),

    # Tax
    "ProvisionForTax": ("tax_cr", True),

    # EPS — same ridiculous tag names as life insurance
    "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized": ("eps", False),
    "BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized": ("eps_diluted", False),
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
# XBRL FORMAT DETECTION
# ═══════════════════════════════════════════════════════════════════

def detect_xbrl_format(vals):
    """
    Detect which XBRL format a filing uses, based on tag presence.
    Order matters: most specific formats first.
    
    Returns: 'insurance_general' | 'insurance' | 'banking' | 'corporate'
    """
    if not vals:
        return 'corporate'
    # General insurance: premium WRITTEN is distinguishing (vs life's premium INCOME)
    # NIACL, ICICIGI, STARHEALTH, etc.
    if 'GrossPremiumsWritten' in vals or 'UnderwritingProfitOrLoss' in vals:
        return 'insurance_general'
    # Life insurance: premium INCOME
    if 'GrossPremiumIncome' in vals or 'NetPremiumIncome' in vals:
        return 'insurance'
    # Banking: NII-based P&L + PPoP is definitive
    if ('InterestEarned' in vals and 
        'OperatingProfitBeforeProvisionAndContingencies' in vals):
        return 'banking'
    # Everything else (corporates + NBFCs that use corporate tags)
    return 'corporate'


def get_tag_map_for_format(fmt):
    """Return the appropriate tag map for a detected XBRL format."""
    if fmt == 'banking':
        return BANKING_QUARTERLY_TAG_MAP
    if fmt == 'insurance':
        return INSURANCE_QUARTERLY_TAG_MAP
    if fmt == 'insurance_general':
        return INSURANCE_GENERAL_QUARTERLY_TAG_MAP
    return QUARTERLY_TAG_MAP


# ═══════════════════════════════════════════════════════════════════
# ERROR LOGGING TO pipeline_failures TABLE
# ═══════════════════════════════════════════════════════════════════

def log_pipeline_failure(supabase, symbol, security_id=None, isin=None,
                         stage=None, failure_type=None, error_message=None,
                         filing_url=None, period_end_date=None,
                         unknown_tags=None, raw_xbrl_snippet=None):
    """
    Write a failure row to pipeline_failures. Never raises — logging must
    not interrupt the pipeline. Silent success, logged warnings on failure.
    """
    try:
        row = {
            "run_date": datetime.now(IST).date().isoformat(),
            "symbol": symbol,
            "security_id": security_id,
            "isin": isin,
            "stage": stage,
            "failure_type": failure_type,
            "error_message": (error_message or "")[:2000],  # Cap long errors
            "filing_url": filing_url,
            "period_end_date": period_end_date,
            "unknown_tags": unknown_tags,
            "raw_xbrl_snippet": (raw_xbrl_snippet or "")[:5000] if raw_xbrl_snippet else None,
            "is_resolved": False,
        }
        # Remove None values to keep DB clean
        row = {k: v for k, v in row.items() if v is not None}
        supabase.table(TABLE_FAILURES).insert(row).execute()
    except Exception as e:
        log.warning(f"Failed to log pipeline_failure for {symbol}: {str(e)[:200]}")


# ═══════════════════════════════════════════════════════════════════
# NSE SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

class NSESession:
    def __init__(self):
        self.session = None
        self.request_count = 0
        self.last_request_time = 0
        self.consecutive_timeouts = 0  # for adaptive rate-limit cooldown
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
        """Fetch a URL with retries and adaptive rate-limit cooldown.

        Rate-limit logic:
          - Each URL gets MAX_RETRIES attempts with exponential backoff on timeout
          - A URL that exhausts all retries counts as ONE "failed URL"
          - After 2 consecutive failed URLs, pause 120s + refresh session
          - After cooldown, the CURRENT URL gets a fresh round of MAX_RETRIES
          - Counter resets to 0 on any successful response
        """
        elapsed = time.time() - self.last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)

        # Two outer passes: normal attempt, then cooldown+retry attempt.
        # Counter tracks URLs that fully failed, not individual retries.
        for pass_num in range(2):
            # If this URL is the 2nd (or later) consecutively failed one, pause
            # and refresh session before retrying. pass_num=1 means we already
            # failed this URL once and are getting a second chance after cooldown.
            if pass_num > 0 or self.consecutive_timeouts >= 2:
                log.warning(
                    f"Rate-limit cooldown: {self.consecutive_timeouts} consecutive "
                    f"failed URL(s). Pausing 120s + refreshing session..."
                )
                time.sleep(120)
                self.consecutive_timeouts = 0
                self.refresh()

            # One full URL attempt: up to MAX_RETRIES inner retries
            url_failed = False
            for attempt in range(MAX_RETRIES):
                try:
                    r = self.session.get(url, timeout=20, **kwargs)
                    self.last_request_time = time.time()
                    self.request_count += 1

                    if r.status_code == 200:
                        self.consecutive_timeouts = 0  # success resets counter
                        return r
                    elif r.status_code == 403:
                        log.warning(f"403 — refreshing session (attempt {attempt+1})")
                        self.refresh()
                        time.sleep(3)
                    elif r.status_code == 429:
                        wait = 10 * (attempt + 1)  # 10, 20, 30, 40, 50s
                        log.warning(f"429 — waiting {wait}s")
                        time.sleep(wait)
                    else:
                        return r  # non-retriable HTTP code, return as-is
                except requests.exceptions.Timeout:
                    log.warning(f"Timeout (attempt {attempt+1}/{MAX_RETRIES})")
                    # Exponential backoff on timeout: 5s, 10s, 20s, 40s, 80s
                    time.sleep(5 * (2 ** attempt))
                except Exception as e:
                    log.warning(f"Request error: {e} (attempt {attempt+1})")
                    time.sleep(3)
            else:
                # Loop exhausted without returning → URL failed all retries
                url_failed = True

            if url_failed:
                self.consecutive_timeouts += 1
                # If this was pass 0 and the counter hit 2, outer loop will
                # do pass 1 (cooldown then retry). Otherwise, give up on URL.
                if pass_num == 0 and self.consecutive_timeouts >= 2:
                    continue  # go to pass 1 (triggers cooldown at top)
                else:
                    return None

        return None


# ═══════════════════════════════════════════════════════════════════
# XBRL PARSING
# ═══════════════════════════════════════════════════════════════════

def classify_xbrl_contexts(root):
    """
    Parse <xbrli:context> elements and classify each contextRef by its period type.
    Returns dict: {context_id: (type, start_date_str, end_date_str)}
      type is one of: 'annual' (~12mo), 'quarterly' (~3mo), 'ytd' (6/9 month),
                      'instant' (snapshot), 'other'
    """
    contexts = {}
    for elem in root.iter():
        tag = elem.tag.split('}')[-1]
        if tag != 'context':
            continue
        ctx_id = elem.get('id')
        if not ctx_id:
            continue

        # Find the <period> child
        period = None
        for child in elem:
            if child.tag.split('}')[-1] == 'period':
                period = child
                break
        if period is None:
            continue

        start = end = instant = None
        for p in period:
            ptag = p.tag.split('}')[-1]
            if ptag == 'startDate' and p.text:
                start = p.text.strip()
            elif ptag == 'endDate' and p.text:
                end = p.text.strip()
            elif ptag == 'instant' and p.text:
                instant = p.text.strip()

        if instant:
            contexts[ctx_id] = ('instant', None, instant)
            continue

        if start and end:
            try:
                sd = datetime.strptime(start, '%Y-%m-%d')
                ed = datetime.strptime(end, '%Y-%m-%d')
                days = (ed - sd).days + 1
                if 340 <= days <= 390:
                    typ = 'annual'
                elif 80 <= days <= 100:
                    typ = 'quarterly'
                elif 165 <= days <= 195:
                    typ = 'h1'  # Half-year (YTD Q2)
                elif 260 <= days <= 285:
                    typ = 'ytd3q'  # 9-month YTD
                else:
                    typ = 'other'
                contexts[ctx_id] = (typ, start, end)
            except Exception:
                contexts[ctx_id] = ('other', start, end)
    return contexts


def parse_xbrl_quarterly(xbrl_url, nse):
    """Parse quarterly XBRL — extracts OneD (current quarter) values.
    Returns (vals, root, raw, fmt, error) — error is None on success,
    otherwise a tuple (failure_type, error_message)."""
    r = nse.get(xbrl_url)
    if not r:
        return None, None, None, None, ('http_error', 'No response from NSE')
    if r.status_code != 200:
        return None, None, None, None, ('http_error', f'HTTP {r.status_code}')

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log.warning(f"XML parse error: {e}")
        return None, None, None, None, ('parse_error', str(e))

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

    if not vals:
        return None, None, None, None, ('no_data_extracted', 'No OneD context values found')

    fmt = detect_xbrl_format(vals)
    return vals, root, raw, fmt, None


def parse_xbrl_annual(xbrl_url, nse):
    """Parse annual XBRL — uses NSE's ordinal-context convention.

    Key insight: NSE encodes period semantics in contextRef NAMES, not in the
    xbrli:period duration (which is always 91 days for both Q4 and annual).
      - contextRef == 'OneD'  → Q4 only (last 3 months)
      - contextRef == 'FourD' → Full fiscal year (Q1+Q2+Q3+Q4 cumulative)
    Both have the same 91-day period markers, so duration-based classification
    (which this function previously attempted) fails.

    Extraction strategy:
      1. P&L and Cash Flow: accept contextRef 'FourD' (the full-year value)
      2. Balance Sheet: accept instant contexts whose date = latest fiscal year-end

    Returns (vals, raw, fmt, error).
    """
    r = nse.get(xbrl_url)
    if not r:
        return None, None, None, ('http_error', 'No response from NSE')
    if r.status_code != 200:
        return None, None, None, ('http_error', f'HTTP {r.status_code}')

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log.warning(f"XML parse error: {e}")
        return None, None, None, ('parse_error', str(e))

    # Classify contexts to find fiscal year end (latest instant date)
    contexts = classify_xbrl_contexts(root)
    if not contexts:
        return None, None, None, ('no_contexts', 'XBRL has no parseable contexts')

    instant_dates = [end for (typ, s, end) in contexts.values()
                     if typ == 'instant' and end]
    fy_end = max(instant_dates) if instant_dates else None

    # Instant contextRefs at fy_end are BS snapshot candidates
    bs_cids = set()
    if fy_end:
        for cid, (typ, s, end) in contexts.items():
            if typ == 'instant' and end == fy_end:
                bs_cids.add(cid)

    vals = {}
    raw = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if not elem.text or not elem.text.strip():
            continue
        if "Reportable" in ctx:
            continue

        # Accept P&L/CF values from FourD (full-year), BS values from matching instants
        is_annual_pl = (ctx == "FourD")
        is_bs_snapshot = (ctx in bs_cids)

        if not (is_annual_pl or is_bs_snapshot):
            continue

        val = elem.text.strip()
        if tag not in vals:
            vals[tag] = val
            raw[tag] = val

    if not vals:
        return None, None, None, ('no_data_extracted',
            f'No values in FourD/instant contexts (fy_end={fy_end})')

    fmt = detect_xbrl_format(vals)
    return vals, raw, fmt, None


def extract_segments(root):
    """
    Extract segment data from XBRL.

    XBRL structure for segments uses contextRef naming like:
      OneReportableSegmentRevenue01D  ← Q4 / current quarter, segment #1
      OneReportableSegmentRevenue02D  ← Q4 / current quarter, segment #2
      FourReportableSegmentRevenue01D ← Full year, segment #1

    So the ORDINAL PREFIX ('One' / 'Four' / etc.) marks the PERIOD (same
    convention as P&L: OneD=Q4, FourD=full year).
    The NUMERIC SUFFIX ('01D' / '02D' / '03D' / ...) marks the SEGMENT INDEX.

    For quarterly XBRLs we want 'One' prefix (current quarter's segment data).
    For annual XBRLs we want 'Four' prefix (full-year segment data).

    Only ONE prefix is handled per call — pass the right root. This function
    selects whichever prefix is most common in Reportable contexts, or 'One'
    as default.
    """
    if root is None:
        return []

    # First check: is this a single-segment company?
    # SEBI requires companies to declare segment structure via:
    #   IsCompanyReportingMultisegmentOrSingleSegment: "Single segment" / "Multisegment"
    #   DescriptionOfSingleSegment: e.g. "Food", "Pharmaceuticals", "NBFC"
    # When single-segment, no Reportable contexts exist — we emit ONE synthetic
    # segment with the company's industry description as the segment name so
    # downstream scoring can see "this company has 1 segment".
    is_single_segment = False
    single_segment_name = None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if not elem.text or not elem.text.strip():
            continue
        if tag == "IsCompanyReportingMultisegmentOrSingleSegment":
            if "single" in elem.text.strip().lower():
                is_single_segment = True
        elif tag == "DescriptionOfSingleSegment":
            if not single_segment_name:
                single_segment_name = elem.text.strip()
        if is_single_segment and single_segment_name:
            break

    if is_single_segment:
        # Return a single synthetic segment. Revenue will be filled by caller
        # using the company's quarterly/annual revenue (since 100% comes from
        # this one segment). Using flag 'is_single_segment' so caller knows.
        return [{
            "segment_name": single_segment_name or "Single Segment",
            "segment_revenue_cr": None,  # Caller fills from company revenue
            "segment_profit_cr": None,
            "segment_assets_cr": None,
            "segment_liabilities_cr": None,
            "is_single_segment": True,
        }]

    # First pass: count how often each ordinal prefix appears in Reportable
    # contexts so we know whether to use 'One' or 'Four' for this filing.
    prefix_counts = {}
    for elem in root.iter():
        ctx = elem.get("contextRef", "")
        if "Reportable" not in ctx:
            continue
        if not (ctx.endswith("1D") or ctx.endswith("01D") or 
                ctx.endswith("2D") or ctx.endswith("02D") or 
                ctx.endswith("3D") or ctx.endswith("03D") or
                ctx.endswith("4D") or ctx.endswith("04D") or
                ctx.endswith("5D") or ctx.endswith("05D") or
                ctx.endswith("6D") or ctx.endswith("06D") or
                ctx.endswith("7D") or ctx.endswith("07D") or
                ctx.endswith("8D") or ctx.endswith("08D") or
                ctx.endswith("9D") or ctx.endswith("09D") or
                ctx.endswith("10D") or ctx.endswith("11D") or ctx.endswith("12D")):
            continue
        m = ORDINAL_PATTERN.match(ctx)
        if not m:
            continue
        prefix = m.group(1)
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1

    if not prefix_counts:
        return []

    # Prefer 'One' (current quarter). Fall back to most common, or 'Four' if
    # that's all we have (annual XBRL).
    if 'One' in prefix_counts:
        chosen_prefix = 'One'
    else:
        chosen_prefix = max(prefix_counts.items(), key=lambda x: x[1])[0]

    # Second pass: extract data, grouping by SUFFIX index (01D, 02D, etc.)
    # Only accept contexts with the chosen prefix.
    # Suffix extraction: last N characters of the form NND or NNNI
    import re as _re
    SUFFIX_RE = _re.compile(r'(\d{1,2})[DI]$')

    segment_data = {}  # suffix (e.g. '01') -> {name, revenue, profit, assets, liabilities}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if not elem.text or not elem.text.strip():
            continue
        if "Reportable" not in ctx:
            continue
        m = ORDINAL_PATTERN.match(ctx)
        if not m:
            continue
        if m.group(1) != chosen_prefix:
            continue

        sm = SUFFIX_RE.search(ctx)
        if not sm:
            continue
        suffix = sm.group(1)  # e.g. '01', '02', '03'

        if suffix not in segment_data:
            segment_data[suffix] = {}

        val = elem.text.strip()

        if tag == "DescriptionOfReportableSegment":
            if "name" not in segment_data[suffix]:
                segment_data[suffix]["name"] = val

        elif tag == "SegmentRevenue":
            if "revenue" not in segment_data[suffix]:
                try:
                    segment_data[suffix]["revenue"] = float(val) / INR_TO_CR
                except (ValueError, TypeError):
                    pass

        elif tag == "SegmentProfitLossBeforeTaxAndFinanceCosts":
            if "profit" not in segment_data[suffix]:
                try:
                    segment_data[suffix]["profit"] = float(val) / INR_TO_CR
                except (ValueError, TypeError):
                    pass

        elif tag == "SegmentAssets":
            if "assets" not in segment_data[suffix]:
                try:
                    segment_data[suffix]["assets"] = float(val) / INR_TO_CR
                except (ValueError, TypeError):
                    pass

        elif tag == "SegmentLiabilities":
            if "liabilities" not in segment_data[suffix]:
                try:
                    segment_data[suffix]["liabilities"] = float(val) / INR_TO_CR
                except (ValueError, TypeError):
                    pass

    # Convert to list, sorted by suffix number
    result = []
    for suffix in sorted(segment_data.keys(), key=lambda x: int(x)):
        data = segment_data[suffix]
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

def fetch_quarterly_filings(symbol, nse):
    """
    Fetch quarterly filings from NSE.
    Strategy: prefer Consolidated, fall back to Standalone if no consolidated exists.
    Some companies (~257) only file Standalone — previously skipped entirely.
    """
    all_filings = []
    consolidated_periods = set()  # Track which periods have consolidated filings

    for from_d, to_d in QUARTERLY_RANGES:
        r = nse.get(
            f"https://www.nseindia.com/api/corporates-financial-results"
            f"?index=equities&period=Quarterly&from_date={from_d}&to_date={to_d}"
        )
        if r and r.status_code == 200:
            try:
                data = r.json()
                # Grab Consolidated first
                consol = [
                    d for d in data
                    if d.get("symbol") == symbol
                    and d.get("consolidated") == "Consolidated"
                    and d.get("xbrl")
                ]
                for f in consol:
                    period_key = f.get("qe_Date") or f.get("toDate")
                    consolidated_periods.add(period_key)
                all_filings.extend(consol)

                # Then Standalone — but only for periods where we don't have consolidated
                standalone = [
                    d for d in data
                    if d.get("symbol") == symbol
                    and d.get("consolidated") in ("Standalone", "Non-Consolidated")
                    and d.get("xbrl")
                    and (d.get("qe_Date") or d.get("toDate")) not in consolidated_periods
                ]
                all_filings.extend(standalone)
            except Exception:
                pass
        time.sleep(REQUEST_DELAY)

    # Integrated filing quarters (2025+)
    r = nse.get(f"https://www.nseindia.com/api/integrated-filing-results?symbol={symbol}")
    if r and r.status_code == 200:
        try:
            data = r.json().get("data", [])
            # Consolidated first
            consol = [
                d for d in data
                if "Financials" in d.get("type", "")
                and d.get("consolidated") == "Consolidated"
                and d.get("xbrl")
            ]
            for f in consol:
                period_key = f.get("qe_Date") or f.get("toDate")
                consolidated_periods.add(period_key)
            all_filings.extend(consol)

            # Standalone fallback for periods without consolidated
            standalone = [
                d for d in data
                if "Financials" in d.get("type", "")
                and d.get("consolidated") in ("Standalone", "Non-Consolidated")
                and d.get("xbrl")
                and (d.get("qe_Date") or d.get("toDate")) not in consolidated_periods
            ]
            all_filings.extend(standalone)
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


def fetch_annual_filings(symbol, nse):
    """
    Fetch annual filings. Same consolidated-first, standalone-fallback strategy.
    Now checks both classic endpoint AND integrated-filing-results (catches
    insurance + newer filings that only appear in the integrated endpoint).
    """
    all_filings = []
    consolidated_periods = set()

    for from_d, to_d in ANNUAL_RANGES:
        r = nse.get(
            f"https://www.nseindia.com/api/corporates-financial-results"
            f"?index=equities&period=Annual&from_date={from_d}&to_date={to_d}"
        )
        if r and r.status_code == 200:
            try:
                data = r.json()
                consol = [
                    d for d in data
                    if d.get("symbol") == symbol
                    and d.get("consolidated") == "Consolidated"
                    and d.get("xbrl")
                ]
                for f in consol:
                    period_key = f.get("qe_Date") or f.get("toDate")
                    consolidated_periods.add(period_key)
                all_filings.extend(consol)

                standalone = [
                    d for d in data
                    if d.get("symbol") == symbol
                    and d.get("consolidated") in ("Standalone", "Non-Consolidated")
                    and d.get("xbrl")
                    and (d.get("qe_Date") or d.get("toDate")) not in consolidated_periods
                ]
                all_filings.extend(standalone)
            except Exception:
                pass
        time.sleep(REQUEST_DELAY)

    # Also check integrated-filing-results for annual filings.
    # Key insight: NSE stopped publishing separate Annual XBRLs after FY24.
    # From FY25 onwards, Q4 Integrated Filings contain full-year data in their
    # FourD context (P&L/CF) and year-end instant context (balance sheet).
    # Our parse_xbrl_annual() already reads FourD + instant contexts, so we
    # just need to feed it Q4 integrated filings by detecting Q4 via qe_Date.
    #
    # Q4 detection: qe_Date ends with a fiscal year-end month. Indian companies
    # use March (most common) or December (calendar-year filers like ABB).
    r = nse.get(f"https://www.nseindia.com/api/integrated-filing-results?symbol={symbol}")
    if r and r.status_code == 200:
        try:
            data = r.json().get("data", [])

            # Collect all period_keys already captured from classic endpoint
            already_covered = set()
            for f in all_filings:
                pk = f.get("qe_Date") or f.get("toDate")
                if pk:
                    already_covered.add(pk)

            def is_annual(d):
                # Accept Q4 integrated Financials filings as annuals — they
                # contain FourD (full-year P&L/CF) and OneI (year-end BS).
                #
                # Q4 detection: qe_Date month is March. 99%+ of Indian
                # companies use April-March fiscal year, so Mar-31 = year-end.
                #
                # Calendar-year filers (ABB, Cummins India, Siemens India etc.
                # who use Jan-Dec fiscal year) will NOT be caught here because
                # we can't distinguish their Q4 (Dec-31) from the Q3 filing
                # (also Dec-31) of April-March filers. Those require per-stock
                # fiscal-year-end metadata. Acknowledged gap, ~5-10 stocks.
                qe = (d.get("qe_Date") or "").upper()
                # Match March only — "31-MAR-2025" or "31-MARCH-2025"
                is_fy_end = ('-MAR-' in qe or qe.endswith('MAR') or '-MARCH-' in qe)
                return ('Financials' in d.get("type", "")
                        and d.get("xbrl")
                        and is_fy_end)

            # Only add filings from periods classic didn't already cover
            consol = [
                d for d in data 
                if is_annual(d) 
                and d.get("consolidated") == "Consolidated"
                and (d.get("qe_Date") or d.get("toDate")) not in already_covered
            ]
            for f in consol:
                period_key = f.get("qe_Date") or f.get("toDate")
                consolidated_periods.add(period_key)
                already_covered.add(period_key)
            all_filings.extend(consol)

            standalone = [
                d for d in data
                if is_annual(d) 
                and d.get("consolidated") in ("Standalone", "Non-Consolidated")
                and (d.get("qe_Date") or d.get("toDate")) not in already_covered
            ]
            all_filings.extend(standalone)
        except Exception:
            pass

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
    """
    Upsert records in batches. Returns count of successfully inserted rows
    (NOT total attempted — only what actually made it to the DB).
    """
    if not records:
        return 0

    success_count = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        try:
            supabase.table(table_name).upsert(batch, on_conflict=on_conflict).execute()
            success_count += len(batch)
        except Exception as e:
            log.error(f"{table_name} write error (batch {i}): {str(e)[:200]}")
        time.sleep(0.2)

    return success_count


# ═══════════════════════════════════════════════════════════════════
# PROCESS SINGLE STOCK
# ═══════════════════════════════════════════════════════════════════

def process_stock(symbol, security_id, isin, nse, supabase):
    """
    Process a single stock: fetch + parse + write all 4 data types.
    Returns stats dict. All failures logged to pipeline_failures table.
    """
    stats = {"quarterly": 0, "annual": 0, "segments": 0, "shareholding": 0}
    formats_seen = set()  # Track which XBRL formats we saw for this stock

    # ─── QUARTERLY P&L + SEGMENTS ───
    try:
        filings = fetch_quarterly_filings(symbol, nse)
    except Exception as e:
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='filing_discovery', failure_type='api_error',
            error_message=f'fetch_quarterly_filings raised: {str(e)}'
        )
        filings = []

    if not filings:
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='filing_discovery', failure_type='no_quarterly_filings',
            error_message='No quarterly XBRL filings found in NSE for any period'
        )

    quarterly_records = []
    segment_records = []

    for filing in filings:
        xbrl_url = filing.get("xbrl", "")
        if not xbrl_url:
            continue

        vals, root, raw, fmt, err = parse_xbrl_quarterly(xbrl_url, nse)
        if err:
            log_pipeline_failure(
                supabase, symbol, security_id, isin,
                stage='xbrl_parse', failure_type=err[0],
                error_message=err[1], filing_url=xbrl_url
            )
            continue

        formats_seen.add(fmt)

        # Determine period dates
        from_date = filing.get("fromDate") or vals.get("DateOfStartOfReportingPeriod")
        to_date = filing.get("toDate") or vals.get("DateOfEndOfReportingPeriod")
        qe_date = filing.get("qe_Date")

        period_end = None
        for src in [to_date, qe_date]:
            if not src:
                continue
            for f in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%b-%y"):
                try:
                    period_end = datetime.strptime(src, f).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            if period_end:
                break

        if not period_end:
            log_pipeline_failure(
                supabase, symbol, security_id, isin,
                stage='xbrl_parse', failure_type='no_period_date',
                error_message='Could not determine period_end_date',
                filing_url=xbrl_url
            )
            continue

        # Route to correct tag map based on format
        tag_map = get_tag_map_for_format(fmt)
        record = map_xbrl_to_record(vals, tag_map)

        # Compute operating profit ONLY for corporates (banks/insurance report directly)
        if fmt == 'corporate':
            compute_operating_profit(record)

        # If extraction produced nothing useful, log it
        if not any(v for v in record.values() if v is not None):
            log_pipeline_failure(
                supabase, symbol, security_id, isin,
                stage='tag_extract', failure_type='no_fields_mapped',
                error_message=f'Format={fmt}, but no tags matched map. XBRL has {len(vals)} tags.',
                filing_url=xbrl_url, period_end_date=period_end,
                unknown_tags=list(vals.keys())[:20]
            )
            continue

        # Is this standalone or consolidated?
        filing_consolidated = filing.get("consolidated", "Consolidated") == "Consolidated"

        # Metadata
        record["security_id"] = security_id
        record["symbol"] = symbol
        record["isin"] = isin
        record["period_end_date"] = period_end
        record["period"] = "Quarterly"
        record["is_consolidated"] = filing_consolidated
        record["is_audited"] = filing.get("audited", "") == "Audited"
        record["has_exceptional_items"] = bool(record.get("exceptional_items_cr"))
        record["nse_symbol"] = symbol
        record["nse_from_date"] = from_date
        record["nse_to_date"] = to_date
        record["xbrl_source_url"] = xbrl_url
        record["filing_date"] = filing.get("broadcastDate", filing.get("broadcastDt"))
        record["raw_xbrl_data"] = json.dumps(raw) if raw else None

        quarterly_records.append(record)

        # Extract segments (only for corporate + banking — insurance doesn't use same structure)
        if fmt in ('corporate', 'banking'):
            segments = extract_segments(root)
            if segments:
                # If this is a single-segment company, fill synthetic segment's
                # revenue with the company's total revenue (100% concentration).
                # Also skip the multi-segment dedup/total-rev math below.
                if len(segments) == 1 and segments[0].get("is_single_segment"):
                    seg = segments[0]
                    company_revenue = record.get("revenue_cr")
                    seg_name = seg["segment_name"]
                    segment_records.append({
                        "security_id": security_id,
                        "symbol": symbol,
                        "isin": isin,
                        "period_end_date": period_end,
                        "is_consolidated": filing_consolidated,
                        "segment_name": seg_name,
                        "segment_canonical_key": canonicalize_segment_name(seg_name),
                        "segment_order": 1,
                        "segment_revenue_cr": company_revenue,
                        "segment_profit_cr": None,
                        "segment_assets_cr": None,
                        "segment_liabilities_cr": None,
                        "segment_revenue_pct": 100.0 if company_revenue else None,
                        "segment_margin_pct": None,
                        "xbrl_source_url": xbrl_url,
                    })
                else:
                    # Multi-segment path: dedup by canonical key — some XBRLs emit
                    # duplicate names across sub-segments (or same segment spelled
                    # twice slightly differently). Keep highest-revenue entry.
                    dedup = {}
                    for s in segments:
                        name = s.get("segment_name")
                        if not name:
                            continue
                        ckey = canonicalize_segment_name(name)
                        if not ckey:
                            continue
                        rev = s.get("segment_revenue_cr") or 0
                        existing = dedup.get(ckey)
                        if existing is None or (rev > (existing.get("segment_revenue_cr") or 0)):
                            # Attach the canonical key for reuse when writing
                            s_copy = dict(s)
                            s_copy["_canonical_key"] = ckey
                            dedup[ckey] = s_copy
                    segments = list(dedup.values())

                    total_rev = sum(s.get("segment_revenue_cr", 0) or 0 for s in segments)
                    for i, seg in enumerate(segments):
                        seg_rev = seg.get("segment_revenue_cr", 0) or 0
                        seg_prof = seg.get("segment_profit_cr", 0) or 0

                        segment_records.append({
                            "security_id": security_id,
                            "symbol": symbol,
                            "isin": isin,
                            "period_end_date": period_end,
                            "is_consolidated": filing_consolidated,
                            "segment_name": seg["segment_name"],
                            "segment_canonical_key": seg.get("_canonical_key"),
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

    try:
        # Dedup quarterly_records by (period_end_date, is_consolidated) — keep first.
        # Protects against same filing appearing in both classic and integrated
        # endpoints with slightly different URLs.
        seen_q_keys = set()
        deduped_quarterly = []
        for q in quarterly_records:
            key = (q["period_end_date"], q["is_consolidated"])
            if key not in seen_q_keys:
                seen_q_keys.add(key)
                deduped_quarterly.append(q)
        stats["quarterly"] = store_records(
            TABLE_QUARTERLY, deduped_quarterly,
            "symbol,period_end_date,is_consolidated", supabase
        )
        # Global dedup on segment_records by (period, segment_name, is_consolidated)
        # Protects against any cross-filing duplicates that survive per-filing dedup
        seen_seg_keys = set()
        deduped_segments = []
        for s in segment_records:
            key = (s["period_end_date"], s["segment_name"], s["is_consolidated"])
            if key not in seen_seg_keys:
                seen_seg_keys.add(key)
                deduped_segments.append(s)
        stats["segments"] = store_records(
            TABLE_SEGMENTS, deduped_segments,
            "symbol,period_end_date,segment_name,is_consolidated", supabase
        )
    except Exception as e:
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='db_insert', failure_type='quarterly_write_error',
            error_message=str(e)
        )

    # ─── ANNUAL P&L + BS + CF ───
    try:
        annual_filings = fetch_annual_filings(symbol, nse)
    except Exception as e:
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='filing_discovery', failure_type='api_error',
            error_message=f'fetch_annual_filings raised: {str(e)}'
        )
        annual_filings = []

    if not annual_filings and quarterly_records:
        # Has quarterly but no annual — worth logging
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='filing_discovery', failure_type='no_annual_filings',
            error_message='Has quarterly data but no annual XBRL filings found'
        )

    annual_records = []

    for filing in annual_filings:
        xbrl_url = filing.get("xbrl", "")
        if not xbrl_url:
            continue

        vals, raw, fmt, err = parse_xbrl_annual(xbrl_url, nse)
        if err:
            log_pipeline_failure(
                supabase, symbol, security_id, isin,
                stage='xbrl_parse', failure_type=err[0],
                error_message=err[1], filing_url=xbrl_url
            )
            continue

        formats_seen.add(fmt)

        from_date = filing.get("fromDate") or vals.get("DateOfStartOfReportingPeriod")
        to_date = filing.get("toDate") or vals.get("DateOfEndOfReportingPeriod")

        period_end = None
        if to_date:
            for f in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    period_end = datetime.strptime(to_date, f).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
        if not period_end:
            log_pipeline_failure(
                supabase, symbol, security_id, isin,
                stage='xbrl_parse', failure_type='no_period_date',
                error_message='Could not determine annual period_end_date',
                filing_url=xbrl_url
            )
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

        # P&L — route by format
        tag_map = get_tag_map_for_format(fmt)
        record = map_xbrl_to_record(vals, tag_map)

        # Map Balance Sheet (common across formats)
        bs_data = map_xbrl_to_record(vals, ANNUAL_BS_TAG_MAP)
        record.update(bs_data)

        # Map Cash Flow (common across formats)
        cf_data = map_xbrl_to_record(vals, ANNUAL_CF_TAG_MAP)
        record.update(cf_data)

        # Compute operating profit ONLY for corporates
        if fmt == 'corporate':
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
        filing_consolidated = filing.get("consolidated", "Consolidated") == "Consolidated"
        record["security_id"] = security_id
        record["symbol"] = symbol
        record["isin"] = isin
        record["period_end_date"] = period_end
        record["fiscal_year"] = fiscal_year
        record["is_consolidated"] = filing_consolidated
        record["is_audited"] = True
        record["nse_symbol"] = symbol
        record["nse_from_date"] = from_date
        record["nse_to_date"] = to_date
        record["xbrl_source_url"] = xbrl_url
        record["raw_xbrl_data"] = json.dumps(raw) if raw else None

        annual_records.append(record)
        time.sleep(REQUEST_DELAY)

    try:
        # Dedup annual_records by (period_end_date, is_consolidated) — keep first.
        seen_a_keys = set()
        deduped_annual = []
        for a in annual_records:
            key = (a["period_end_date"], a["is_consolidated"])
            if key not in seen_a_keys:
                seen_a_keys.add(key)
                deduped_annual.append(a)
        stats["annual"] = store_records(
            TABLE_ANNUAL, deduped_annual,
            "symbol,period_end_date,is_consolidated", supabase
        )
    except Exception as e:
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='db_insert', failure_type='annual_write_error',
            error_message=str(e)
        )

    # ─── SHAREHOLDING ───
    try:
        shp_data = fetch_shareholding(symbol, nse)
    except Exception as e:
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='filing_discovery', failure_type='api_error',
            error_message=f'fetch_shareholding raised: {str(e)}'
        )
        shp_data = []

    shp_records = []

    for entry in shp_data:
        date_str = entry.get("date", "")
        # Fix: NSE sometimes returns None or non-string for date — skip gracefully
        if not isinstance(date_str, str) or not date_str.strip():
            continue
        period_end = None
        for f in ("%d-%b-%Y", "%d-%B-%Y"):
            try:
                period_end = datetime.strptime(date_str, f).strftime("%Y-%m-%d")
                break
            except (ValueError, TypeError):
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
            "isin": isin,
            "period_end_date": period_end,
            "period": "Quarterly",
            "promoter_percent": float(promoter) if promoter else None,
            "public_percent": float(public) if public else None,
            "employee_trusts_percent": float(entry.get("employeeTrusts", 0)) if entry.get("employeeTrusts") else None,
            "xbrl_source_url": entry.get("xbrl"),
            "source_url": entry.get("xbrl"),
            "submission_date": entry.get("submissionDate"),
        })

    try:
        # Dedup shp_records by period_end_date — NSE sometimes has multiple
        # shareholding entries for the same period (revisions). Keep first.
        seen_sh_keys = set()
        deduped_sh = []
        for sh in shp_records:
            key = sh["period_end_date"]
            if key not in seen_sh_keys:
                seen_sh_keys.add(key)
                deduped_sh.append(sh)
        stats["shareholding"] = store_records(
            TABLE_SHAREHOLDING, deduped_sh,
            "symbol,period_end_date", supabase
        )
    except Exception as e:
        log_pipeline_failure(
            supabase, symbol, security_id, isin,
            stage='db_insert', failure_type='shareholding_write_error',
            error_message=str(e)
        )

    stats["formats"] = formats_seen
    return stats


# ═══════════════════════════════════════════════════════════════════
# RESUME CHECK — checks v2 tables (for restartable runs)
# ═══════════════════════════════════════════════════════════════════

def is_stock_processed(symbol, supabase):
    """Check if stock already has data in v2 tables (for mid-run restart)."""
    try:
        r = (
            supabase.table(TABLE_QUARTERLY)
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
    """
    Load the active equity universe with ISIN for pipeline input.
    Only loads non-ETF, active, EQ series stocks.
    """
    all_stocks = []
    offset = 0

    while True:
        query = (
            supabase.table("stock_universe")
            .select("security_id,symbol,isin,company_name")
            .eq("is_active", True)
            .eq("is_etf", False)
            .in_("nse_series", ["EQ", "BE"])
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
    log.info("  VALVO Intelligence — NSE Fundamentals Pipeline v2 (FRESH RUN)")
    log.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    log.info(f"  Run ID: {RUN_ID}")
    log.info(f"  Target tables: {TABLE_QUARTERLY}, {TABLE_ANNUAL},")
    log.info(f"                 {TABLE_SEGMENTS}, {TABLE_SHAREHOLDING}")
    log.info(f"  Failures logged to: {TABLE_FAILURES}")
    log.info(f"  XBRL formats: corporate | banking | insurance (auto-detected)")
    if specific_symbols:
        log.info(f"  Mode: Specific stocks — {', '.join(specific_symbols)}")
    else:
        log.info("  Mode: Full universe fresh run")
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
    format_counts = {"corporate": 0, "banking": 0, "insurance": 0, "insurance_general": 0}
    start_time = time.time()
    failed_symbols = []

    for i, stock in enumerate(stocks):
        symbol = stock["symbol"]
        security_id = stock["security_id"]
        isin = stock.get("isin")

        # Resume: skip if already has data in v2 (for mid-run restart)
        if not specific_symbols and is_stock_processed(symbol, supabase):
            skipped += 1
            if skipped <= 10 or skipped % 100 == 0:
                log.info(f"  [{i+1}/{total}] {symbol}: skipped (already in v2)")
            continue

        # Refresh session periodically
        if nse.request_count > 0 and nse.request_count % (SESSION_REFRESH_EVERY * 10) == 0:
            nse.refresh()

        # Process
        try:
            stats = process_stock(symbol, security_id, isin, nse, supabase)

            total_q += stats["quarterly"]
            total_a += stats["annual"]
            total_s += stats["segments"]
            total_sh += stats["shareholding"]

            # Track formats seen
            for fmt in stats.get("formats", set()):
                if fmt in format_counts:
                    format_counts[fmt] += 1

            if sum(v for k, v in stats.items() if k != "formats") > 0:
                processed += 1
                fmts = ','.join(stats.get("formats", []) or ['none'])
                log.info(
                    f"  ✅ [{i+1}/{total}] {symbol} ({fmts}): "
                    f"Q={stats['quarterly']} A={stats['annual']} "
                    f"S={stats['segments']} SH={stats['shareholding']}"
                )
            else:
                failed += 1
                failed_symbols.append(symbol)
                log.warning(f"  ⏭️ [{i+1}/{total}] {symbol}: no data extracted (see pipeline_failures)")

        except Exception as e:
            failed += 1
            failed_symbols.append(symbol)
            log.error(f"  ❌ [{i+1}/{total}] {symbol}: {str(e)[:100]}")
            # Log to pipeline_failures
            log_pipeline_failure(
                supabase, symbol, security_id, isin,
                stage='process_stock', failure_type='unexpected_exception',
                error_message=str(e)
            )

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
                f"     Formats seen — corp:{format_counts['corporate']} "
                f"bank:{format_counts['banking']} "
                f"ins_life:{format_counts['insurance']} "
                f"ins_gen:{format_counts['insurance_general']}\n"
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
    log.info(f"  Corporate format       : {format_counts['corporate']} stocks")
    log.info(f"  Banking format         : {format_counts['banking']} stocks")
    log.info(f"  Life insurance format  : {format_counts['insurance']} stocks")
    log.info(f"  General insurance fmt  : {format_counts['insurance_general']} stocks")
    log.info(f"  NSE requests       : {nse.request_count}")
    log.info(f"  Time               : {elapsed_total/60:.1f} minutes")
    log.info(f"  Log                : {log_file}")
    log.info(f"  Failures table     : query pipeline_failures WHERE run_date='{now.date()}'")
    log.info("=" * 70)

    if failed_symbols:
        log.info(f"\n  Failed ({len(failed_symbols)}):")
        for c in range(0, len(failed_symbols), 20):
            log.info(f"    {', '.join(failed_symbols[c:c+20])}")


if __name__ == "__main__":
    main()
