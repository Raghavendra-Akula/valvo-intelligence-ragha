"""
Inspect TCS XBRL segments: what does extract_segments find?
What goes wrong between extraction and DB insert?
"""
import sys, os
sys.path.insert(0, os.path.expanduser('~/fundamentals'))
from run_pipeline_v2 import NSESession, extract_segments, ORDINAL_PATTERN
from xml.etree import ElementTree as ET

nse = NSESession()

# TCS Q3 FY25 — known 6-segment stock
url = "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1595521_09012025115254_WEB.xml"
r = nse.get(url)
if not r or r.status_code != 200:
    # Fallback — recent TCS XBRL
    urls = [
        "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_119179_1375214_07022025085152.xml",  # random
    ]
    for u in urls:
        r = nse.get(u)
        if r and r.status_code == 200:
            url = u
            break

print(f"Fetched: {url}")
print(f"Size: {len(r.content)} bytes\n")

root = ET.fromstring(r.content)

# 1. Find ALL Reportable contexts
reportable_ctxs = set()
for elem in root.iter():
    ctx = elem.get("contextRef", "")
    if "Reportable" in ctx:
        reportable_ctxs.add(ctx)

print(f"Unique Reportable contextRefs: {len(reportable_ctxs)}")
for ctx in sorted(reportable_ctxs)[:30]:
    print(f"  {ctx}")
print()

# 2. What ordinals exist?
ordinals_found = set()
for ctx in reportable_ctxs:
    m = ORDINAL_PATTERN.match(ctx)
    if m:
        ordinals_found.add(m.group(1))

print(f"Ordinals found in Reportable contexts: {sorted(ordinals_found)}")
print()

# 3. What does extract_segments produce?
print("=" * 70)
print("extract_segments() output:")
print("=" * 70)
segments = extract_segments(root)
for i, s in enumerate(segments):
    print(f"  #{i+1}: name={s['segment_name'][:50]!r}, revenue={s.get('segment_revenue_cr')}")
print(f"\nTotal segments extracted: {len(segments)}")

# 4. If only 1 result, why? Let's see what each ordinal actually has
if len(segments) < 3:
    print("\n" + "=" * 70)
    print("DEEP DIVE — raw segment_data before filtering:")
    print("=" * 70)
    from run_pipeline_v2 import ORDINAL_ORDER, INR_TO_CR
    segment_data = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        ctx = elem.get("contextRef", "")
        if not elem.text or not elem.text.strip():
            continue
        if "Reportable" not in ctx:
            continue
        if not (ctx.endswith("1D") or ctx.endswith("01D")):
            continue
        m = ORDINAL_PATTERN.match(ctx)
        if not m:
            continue
        ordinal = m.group(1)
        if ordinal not in segment_data:
            segment_data[ordinal] = {}
        val = elem.text.strip()
        if tag == "DescriptionOfReportableSegment":
            if "name" not in segment_data[ordinal]:
                segment_data[ordinal]["name"] = val
            else:
                if segment_data[ordinal]["name"] != val:
                    print(f"    CONFLICT on {ordinal}: {segment_data[ordinal]['name']} vs {val}")
        elif tag == "SegmentRevenue":
            if "revenue" not in segment_data[ordinal]:
                segment_data[ordinal]["revenue"] = float(val)/INR_TO_CR

    print(f"\n{len(segment_data)} ordinals captured:")
    for ordinal in sorted(segment_data.keys(), key=lambda x: ORDINAL_ORDER.get(x, 99)):
        d = segment_data[ordinal]
        print(f"  {ordinal}: name={d.get('name', 'NONE')!r}, revenue={d.get('revenue', 'NONE')}")
