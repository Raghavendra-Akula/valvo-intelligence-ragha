"""
Validate new extract_segments on a known multi-segment stock.
Also test on the single-segment case that originally revealed the bug.
"""
import sys, os
sys.path.insert(0, os.path.expanduser('~/fundamentals'))
from run_pipeline_v2 import NSESession, extract_segments, fetch_quarterly_filings
from xml.etree import ElementTree as ET

nse = NSESession()

# Try to find a fresh TCS XBRL for Q3 FY25 (Dec 2024)
print("Fetching TCS filings...")
filings = fetch_quarterly_filings("TCS", nse)

# Find one with period ending 2024-12-31
tcs_url = None
for f in filings:
    qe = f.get("qe_Date", "") or f.get("toDate", "")
    if "31-Dec-2024" in qe or "31-12-2024" in qe or "2024-12-31" in qe:
        if f.get("consolidated") == "Consolidated":
            tcs_url = f.get("xbrl", "")
            break

if not tcs_url:
    print("Couldn't find TCS Q3 FY25 — using most recent")
    tcs_url = filings[0].get("xbrl", "") if filings else None

print(f"\nTCS URL: {tcs_url}")
if tcs_url:
    r = nse.get(tcs_url)
    if r and r.status_code == 200:
        root = ET.fromstring(r.content)
        segments = extract_segments(root)
        print(f"\nTCS extracted segments: {len(segments)}")
        for i, s in enumerate(segments, 1):
            rev = s.get("segment_revenue_cr")
            prof = s.get("segment_profit_cr")
            print(f"  #{i}: {s['segment_name'][:45]:45s}  rev={rev}  profit={prof}")
        total = sum(s.get("segment_revenue_cr") or 0 for s in segments)
        print(f"\n  Total segment revenue: {total:.1f} Cr")

# Also test the single-segment case (the Paints one from before)
print("\n" + "="*70)
print("Single-segment case (Paints XBRL):")
paints_url = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_119179_1375214_07022025085152.xml"
r = nse.get(paints_url)
if r and r.status_code == 200:
    root = ET.fromstring(r.content)
    segments = extract_segments(root)
    print(f"\nExtracted segments: {len(segments)}")
    for i, s in enumerate(segments, 1):
        print(f"  #{i}: {s['segment_name']:20s}  rev={s.get('segment_revenue_cr')}")
