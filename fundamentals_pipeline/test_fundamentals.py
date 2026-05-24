"""
VALVO — Complete Fundamental Data Test
========================================
Pulls ALL available data from NSE for a single company.
Tests: Quarterly P&L, Annual BS+CF, Segments, Shareholding.

Usage: python3 test_fundamentals.py TCS
"""

import sys
import time
import requests
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "TCS"


def get_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })
    s.get("https://www.nseindia.com")
    time.sleep(1)
    return s


def parse_xbrl_one_d(xbrl_url, session):
    """Download XBRL and extract OneD (current quarter) values."""
    r = session.get(xbrl_url, timeout=15)
    root = ET.fromstring(r.content)
    vals = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if "OneD" in ctx and elem.text and elem.text.strip():
            if tag not in vals:
                vals[tag] = elem.text.strip()
    return vals, root


def parse_xbrl_all(xbrl_url, session):
    """Download XBRL and extract ALL values (for annual filings)."""
    r = session.get(xbrl_url, timeout=15)
    root = ET.fromstring(r.content)
    vals = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if elem.text and elem.text.strip():
            if tag not in vals:
                vals[tag] = elem.text.strip()
    return vals, root


def smart_divisor(val):
    """Determine if values are in rupees or lakhs and return divisor to get crores."""
    if abs(val) > 1000000000:
        return 10000000  # Rupees to Crores
    elif abs(val) > 10000000:
        return 100000  # Lakhs to Crores
    else:
        return 100  # Already in lakhs? / 100 = Cr


def to_cr(val_str):
    """Convert string to crores."""
    try:
        num = float(val_str)
        if abs(num) > 1000000000:
            return num / 10000000
        elif abs(num) > 10000000:
            return num / 100000
        else:
            return num
    except:
        return 0


def section_quarterly(session):
    """Fetch all quarterly P&L data."""
    print("\n" + "=" * 90)
    print("  1. QUARTERLY P&L (Historical + Integrated)")
    print("=" * 90)

    all_quarters = []

    # Historical quarters (2018-2025)
    ranges = [
        ("01-04-2018", "31-03-2019"),
        ("01-04-2019", "31-03-2020"),
        ("01-04-2020", "31-03-2021"),
        ("01-04-2021", "31-03-2022"),
        ("01-04-2022", "31-03-2023"),
        ("01-04-2023", "31-03-2024"),
        ("01-04-2024", "31-03-2025"),
    ]

    for from_d, to_d in ranges:
        time.sleep(0.5)
        url = f"https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly&from_date={from_d}&to_date={to_d}"
        r = session.get(url)
        if r.status_code == 200:
            data = r.json()
            matched = [
                d for d in data
                if d.get("symbol") == SYMBOL
                and d.get("consolidated") == "Consolidated"
                and d.get("xbrl")
            ]
            all_quarters.extend(matched)

    # Integrated filing quarters (2025+)
    time.sleep(0.5)
    r = session.get(f"https://www.nseindia.com/api/integrated-filing-results?symbol={SYMBOL}")
    if r.status_code == 200:
        data = r.json().get("data", [])
        consol = [
            d for d in data
            if "Financials" in d.get("type", "")
            and d.get("consolidated") == "Consolidated"
            and d.get("xbrl")
        ]
        all_quarters.extend(consol)

    # Deduplicate by XBRL URL
    seen = set()
    unique = []
    for q in all_quarters:
        url = q.get("xbrl", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(q)

    print(f"\n  Total unique consolidated quarters: {len(unique)}\n")
    header = f"  {'Period':30s} | {'Revenue':>10s} | {'Expenses':>10s} | {'PAT':>10s} | {'EPS':>8s} | {'OPM%':>6s}"
    print(header)
    print("  " + "-" * 85)

    for q in sorted(unique, key=lambda x: x.get("fromDate", x.get("qe_Date", ""))):
        time.sleep(0.3)
        try:
            vals, _ = parse_xbrl_one_d(q["xbrl"], session)
            rev = to_cr(vals.get("RevenueFromOperations", "0"))
            exp = to_cr(vals.get("Expenses", "0"))
            pat = to_cr(vals.get("ProfitLossForPeriod", "0"))
            opm = ((rev - exp) / rev * 100) if rev else 0
            eps = vals.get("BasicEarningsLossPerShareFromContinuingOperations", "-")

            fd = q.get("fromDate", q.get("qe_Date", "?"))
            td = q.get("toDate", "")
            period = f"{fd} to {td}" if td else fd
            print(f"  {period:30s} | {rev:>8,.0f}Cr | {exp:>8,.0f}Cr | {pat:>8,.0f}Cr | {eps:>8s} | {opm:5.1f}%")
        except Exception as e:
            print(f"  ERROR: {str(e)[:60]}")

    return unique


def section_detailed_pl(session, quarters):
    """Show detailed P&L breakdown for latest quarter."""
    print("\n" + "=" * 90)
    print("  2. DETAILED P&L BREAKDOWN (Latest Quarter)")
    print("=" * 90)

    if not quarters:
        print("  No quarters found")
        return

    latest = sorted(quarters, key=lambda x: x.get("fromDate", x.get("qe_Date", "")))[-1]
    vals, _ = parse_xbrl_one_d(latest["xbrl"], session)

    pl_tags = [
        "RevenueFromOperations", "OtherIncome", "Income",
        "CostOfMaterialsConsumed", "PurchasesOfStockInTrade",
        "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
        "EmployeeBenefitExpense", "FinanceCosts",
        "DepreciationDepletionAndAmortisationExpense", "OtherExpenses", "Expenses",
        "ProfitBeforeExceptionalItemsAndTax", "ExceptionalItemsBeforeTax", "ProfitBeforeTax",
        "CurrentTax", "DeferredTax", "TaxExpense",
        "ProfitLossForPeriod",
        "ProfitOrLossAttributableToOwnersOfParent",
        "ProfitOrLossAttributableToNonControllingInterests",
        "OtherComprehensiveIncomeNetOfTaxes", "ComprehensiveIncomeForThePeriod",
        "PaidUpValueOfEquityShareCapital", "FaceValueOfEquityShareCapital",
        "BasicEarningsLossPerShareFromContinuingOperations",
        "DilutedEarningsLossPerShareFromContinuingOperations",
        "DebtEquityRatio", "DebtServiceCoverageRatio", "InterestServiceCoverageRatio",
    ]

    print()
    for tag in pl_tags:
        if tag in vals:
            try:
                num = float(vals[tag])
                if abs(num) > 1000000:
                    print(f"  {tag:60s} | Rs {num/10000000:>12,.2f} Cr")
                else:
                    print(f"  {tag:60s} | {vals[tag]:>15s}")
            except:
                print(f"  {tag:60s} | {vals[tag][:20]:>15s}")

    return latest


def section_segments(session, latest):
    """Show segment breakdown for latest quarter."""
    print("\n" + "=" * 90)
    print("  3. SEGMENT BREAKDOWN (Latest Quarter)")
    print("=" * 90)

    if not latest:
        print("  No data")
        return

    _, root = parse_xbrl_one_d(latest["xbrl"], session)

    segments = {}
    current_seg = None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if not elem.text or not elem.text.strip():
            continue

        if "Reportable" in ctx and "1D" in ctx:
            if tag == "DescriptionOfReportableSegment":
                current_seg = elem.text.strip()
                if current_seg not in segments:
                    segments[current_seg] = {}
            elif current_seg and tag in (
                "SegmentRevenue",
                "SegmentProfitLossBeforeTaxAndFinanceCosts",
                "SegmentAssets",
                "SegmentLiabilities",
            ):
                try:
                    segments[current_seg][tag] = float(elem.text.strip()) / 10000000
                except:
                    pass

    if segments:
        print(f"\n  {'Segment':30s} | {'Revenue':>10s} | {'Profit':>10s} | {'Assets':>10s} | {'Margin':>7s}")
        print("  " + "-" * 80)
        for seg, data in sorted(segments.items(), key=lambda x: x[1].get("SegmentRevenue", 0), reverse=True):
            rev = data.get("SegmentRevenue", 0)
            prof = data.get("SegmentProfitLossBeforeTaxAndFinanceCosts", 0)
            assets = data.get("SegmentAssets", 0)
            margin = (prof / rev * 100) if rev else 0
            if rev or prof or assets:
                print(f"  {seg:30s} | {rev:>8,.0f}Cr | {prof:>8,.0f}Cr | {assets:>8,.0f}Cr | {margin:5.1f}%")
    else:
        print("\n  Single segment company or no segment data found")


def section_annual(session):
    """Fetch annual filings with Balance Sheet and Cash Flow."""
    print("\n" + "=" * 90)
    print("  4. ANNUAL FILINGS (P&L + Balance Sheet + Cash Flow)")
    print("=" * 90)

    annual_filings = []
    annual_ranges = [
        ("01-04-2019", "31-03-2020"),
        ("01-04-2020", "31-03-2021"),
        ("01-04-2021", "31-03-2022"),
        ("01-04-2022", "31-03-2023"),
        ("01-04-2023", "31-03-2024"),
        ("01-04-2024", "31-03-2025"),
    ]

    for from_d, to_d in annual_ranges:
        time.sleep(0.5)
        url = f"https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Annual&from_date={from_d}&to_date={to_d}"
        r = session.get(url)
        if r.status_code == 200:
            data = r.json()
            matched = [
                d for d in data
                if d.get("symbol") == SYMBOL
                and d.get("consolidated") == "Consolidated"
                and d.get("xbrl")
            ]
            annual_filings.extend(matched)

    # Deduplicate
    seen = set()
    unique = []
    for a in annual_filings:
        url = a.get("xbrl", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(a)

    print(f"\n  Annual filings found: {len(unique)}")

    bs_tags = [
        "Assets", "NoncurrentAssets", "CurrentAssets",
        "Equity", "EquityShareCapital", "OtherEquity",
        "NoncurrentLiabilities", "CurrentLiabilities",
        "BorrowingsNoncurrent", "BorrowingsCurrent",
        "TradeReceivablesCurrent", "TradePayablesCurrent",
        "Inventories", "CashAndCashEquivalents",
        "PropertyPlantAndEquipment", "CapitalWorkInProgress",
        "Goodwill", "OtherIntangibleAssets",
        "CurrentInvestments", "NoncurrentInvestments",
    ]

    cf_tags = [
        "CashFlowsFromUsedInOperatingActivities",
        "CashFlowsFromUsedInInvestingActivities",
        "CashFlowsFromUsedInFinancingActivities",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "DividendsPaidClassifiedAsFinancingActivities",
        "InterestPaidClassifiedAsFinancingActivities",
        "IncomeTaxesPaidRefundClassifiedAsOperatingActivities",
    ]

    for af in sorted(unique, key=lambda x: x.get("fromDate", "")):
        time.sleep(0.3)
        try:
            vals, _ = parse_xbrl_all(af["xbrl"], session)

            fd = af.get("fromDate", vals.get("DateOfStartOfReportingPeriod", "?"))
            td = af.get("toDate", vals.get("DateOfEndOfReportingPeriod", "?"))

            rev = to_cr(vals.get("RevenueFromOperations", "0"))
            pat = to_cr(vals.get("ProfitLossForPeriod", "0"))

            print(f"\n  FY: {fd} to {td}")
            print(f"  Revenue: {rev:,.0f} Cr | PAT: {pat:,.0f} Cr")

            # Balance Sheet
            print(f"\n  {'--- Balance Sheet ---':60s}")
            for tag in bs_tags:
                if tag in vals:
                    try:
                        num = float(vals[tag])
                        if abs(num) > 100000:
                            print(f"    {tag:55s} | Rs {num/10000000:>10,.0f} Cr")
                    except:
                        pass

            # Cash Flow
            print(f"\n  {'--- Cash Flow ---':60s}")
            for tag in cf_tags:
                if tag in vals:
                    try:
                        num = float(vals[tag])
                        if abs(num) > 100000:
                            print(f"    {tag:55s} | Rs {num/10000000:>10,.0f} Cr")
                    except:
                        pass

        except Exception as e:
            print(f"  ERROR: {str(e)[:60]}")


def section_shareholding(session):
    """Fetch shareholding pattern."""
    print("\n" + "=" * 90)
    print("  5. SHAREHOLDING PATTERN (Last 8 Quarters)")
    print("=" * 90)

    time.sleep(0.5)
    r = session.get(
        f"https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={SYMBOL}"
    )
    if r.status_code == 200:
        shp = r.json()
        print(f"\n  {'Quarter':15s} | {'Promoter':>10s} | {'Public':>10s}")
        print("  " + "-" * 45)
        for q in shp[:8]:
            pr = q.get("pr_and_prgrp", "-")
            pub = q.get("public_val", "-")
            print(f"  {q['date']:15s} | {pr:>9s}% | {pub:>9s}%")
    else:
        print(f"  Failed: HTTP {r.status_code}")


def main():
    now = datetime.now(IST)
    print("=" * 90)
    print(f"  VALVO — COMPLETE FUNDAMENTAL DATA TEST: {SYMBOL}")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 90)

    session = get_session()

    # 1. Quarterly P&L
    quarters = section_quarterly(session)

    # 2. Detailed P&L
    latest = section_detailed_pl(session, quarters)

    # 3. Segments
    section_segments(session, latest)

    # 4. Annual filings
    section_annual(session)

    # 5. Shareholding
    section_shareholding(session)

    print("\n" + "=" * 90)
    print("  TEST COMPLETE")
    print("=" * 90)


if __name__ == "__main__":
    main()
