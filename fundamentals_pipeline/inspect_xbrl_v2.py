"""
Full XBRL diagnostic: map all 53 contexts by period duration.
Also test a newer (FY25) TCS XBRL from integrated-filing-results endpoint
to see if newer XBRLs have proper annual contexts.
"""
import sys, os
sys.path.insert(0, os.path.expanduser('~/fundamentals'))
from run_pipeline_v2 import NSESession, classify_xbrl_contexts
from xml.etree import ElementTree as ET
from datetime import datetime
from collections import Counter

nse = NSESession()

urls = [
    ("TCS FY20 (classic endpoint)", "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_54905_240070_16042020082728_WEB.xml"),
    ("TCS FY24 (classic endpoint)", "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_104549_1090527_12042024090154.xml"),
]

for label, url in urls:
    print("="*70)
    print(f"  {label}")
    print(f"  {url[-60:]}")
    print("="*70)
    r = nse.get(url)
    if not r or r.status_code != 200:
        print(f"  Failed to fetch: {r.status_code if r else 'no response'}")
        continue
    print(f"  Size: {len(r.content)} bytes\n")

    root = ET.fromstring(r.content)

    # Categorize all contexts
    type_counts = Counter()
    duration_samples = {}
    for elem in root.iter():
        localname = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if localname != 'context':
            continue
        ctx_id = elem.get('id', '?')

        # Find period
        period = None
        for child in elem:
            if (child.tag.split('}')[-1] if '}' in child.tag else child.tag) == 'period':
                period = child; break
        if period is None:
            type_counts['no_period'] += 1
            continue

        start = end = instant = None
        for p in period:
            ptag = p.tag.split('}')[-1] if '}' in p.tag else p.tag
            if ptag == 'startDate' and p.text: start = p.text.strip()
            elif ptag == 'endDate' and p.text: end = p.text.strip()
            elif ptag == 'instant' and p.text: instant = p.text.strip()

        if instant:
            type_counts[f"instant:{instant}"] += 1
            continue

        if start and end:
            try:
                sd = datetime.strptime(start, '%Y-%m-%d')
                ed = datetime.strptime(end, '%Y-%m-%d')
                days = (ed - sd).days + 1
                key = f"{days}d: {start}→{end}"
                type_counts[key] += 1
                if days not in duration_samples:
                    duration_samples[days] = ctx_id
            except: 
                type_counts['parse_err'] += 1

    print("  Context period distribution:")
    for k, v in sorted(type_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"    {v:3d}x  {k}")

    # Now check RevenueFromOperations contextRefs
    print("\n  contextRefs used on 'RevenueFromOperations' tag:")
    for elem in root.iter():
        tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if tag == 'RevenueFromOperations':
            ref = elem.get('contextRef', '?')
            val = (elem.text or '').strip()[:30]
            print(f"    contextRef='{ref}'  value={val}")

    # What our new classifier says
    ctxs = classify_xbrl_contexts(root)
    type_breakdown = Counter(t[0] for t in ctxs.values())
    print(f"\n  classify_xbrl_contexts() breakdown: {dict(type_breakdown)}")
    print()
