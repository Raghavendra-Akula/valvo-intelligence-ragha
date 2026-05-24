"""
Diagnostic: Fetch TCS FY20 XBRL and dump its context structure so we can see
why classify_xbrl_contexts isn't matching anything.
"""
import sys
import os
sys.path.insert(0, os.path.expanduser('~/fundamentals'))
from run_pipeline_v2 import NSESession
from xml.etree import ElementTree as ET

nse = NSESession()
url = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_54905_240070_16042020082728_WEB.xml"
r = nse.get(url)
print(f"HTTP {r.status_code}, {len(r.content)} bytes\n")

root = ET.fromstring(r.content)

# 1. Print root tag + namespaces
print(f"Root tag: {root.tag}")
print(f"Root nsmap (first 5): {dict(list(root.attrib.items())[:5])}\n")

# 2. Find all elements with 'context' in their local name
print("=== Elements matching local name 'context' ===")
count = 0
for elem in root.iter():
    localname = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
    if localname == 'context':
        count += 1
        if count <= 3:
            print(f"\nContext element #{count}:")
            print(f"  Full tag: {elem.tag}")
            print(f"  id attr: {elem.get('id')}")
            print(f"  Children:")
            for child in elem:
                child_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                print(f"    <{child_local}>")
                for grand in child:
                    gl = grand.tag.split('}')[-1] if '}' in grand.tag else grand.tag
                    print(f"      <{gl}> = {grand.text!r}" if grand.text else f"      <{gl}>")

print(f"\nTotal <context> elements found: {count}")

# 3. Sample first few unique contextRef values seen on data elements
print("\n=== Sample contextRefs seen on data elements ===")
seen_refs = set()
for elem in root.iter():
    ref = elem.get('contextRef')
    if ref and ref not in seen_refs:
        seen_refs.add(ref)
        if len(seen_refs) <= 15:
            print(f"  contextRef='{ref}'")

print(f"\nTotal unique contextRefs: {len(seen_refs)}")
