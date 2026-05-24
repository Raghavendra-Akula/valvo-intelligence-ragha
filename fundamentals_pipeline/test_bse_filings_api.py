"""
Quick test: Does BSE API return filings for HDFC Bank, L&T, Sun Pharma?
Run this on VM or Mac:
    python3 test_bse_filings_api.py
"""
import requests
import json
import time

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/corporates/ann.html",
    "Origin": "https://www.bseindia.com",
}

BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

TEST_COMPANIES = [
    ("500180", "HDFCBANK"),
    ("500510", "LT"),
    ("524715", "SUNPHARMA"),
]

def test():
    # Step 1: Create session with cookies
    session = requests.Session()
    session.headers.update(HEADERS)
    
    print("1. Visiting BSE homepage for cookies...")
    resp = session.get("https://www.bseindia.com/", timeout=15)
    print(f"   Homepage: {resp.status_code}, cookies: {len(session.cookies)}")
    time.sleep(2)
    
    resp = session.get("https://www.bseindia.com/corporates/ann.html", timeout=15)
    print(f"   Ann page: {resp.status_code}")
    time.sleep(2)
    
    # Step 2: Test each company
    for bse_code, symbol in TEST_COMPANIES:
        print(f"\n{'='*50}")
        print(f"2. Testing {symbol} (BSE: {bse_code})...")
        
        params = {
            "pageno": "1",
            "strCat": "-1",
            "strPrevDate": "2024-01-01",
            "strScrip": bse_code,
            "strSearch": "P",
            "strToDate": "20260408",
            "strType": "C",
        }
        
        try:
            resp = session.get(BSE_URL, params=params, timeout=30)
            content_type = resp.headers.get('Content-Type', '')
            
            print(f"   Status: {resp.status_code}")
            print(f"   Content-Type: {content_type}")
            print(f"   Response size: {len(resp.text)} chars")
            
            # Check if HTML (error) or JSON (success)
            if 'html' in content_type.lower() or resp.text.strip().startswith('<!'):
                print(f"   ⚠️  GOT HTML — BSE is blocking/throttling!")
                print(f"   First 200 chars: {resp.text[:200]}")
            else:
                try:
                    data = resp.json()
                    rows = data if isinstance(data, list) else data.get('Table', [])
                    print(f"   ✅ Got {len(rows)} filings on page 1")
                    
                    if rows:
                        # Show first filing
                        first = rows[0]
                        print(f"   First filing:")
                        print(f"     Category: {first.get('CATEGORYNAME')}")
                        print(f"     SubCat:   {first.get('SUBCATNAME')}")
                        print(f"     Date:     {first.get('DisssemDT', first.get('NEWS_DT'))}")
                        print(f"     Subject:  {str(first.get('NEWSSUB', ''))[:100]}")
                        print(f"     Attach:   {first.get('ATTACHMENTNAME', 'None')[:60]}")
                        print(f"     NewsID:   {first.get('NEWSID')}")
                        
                        # Count by category
                        cats = {}
                        for r in rows:
                            cat = r.get('CATEGORYNAME', 'Unknown')
                            cats[cat] = cats.get(cat, 0) + 1
                        print(f"   Categories: {cats}")
                        
                except json.JSONDecodeError:
                    print(f"   ⚠️  Response is not valid JSON!")
                    print(f"   First 200 chars: {resp.text[:200]}")
                    
        except Exception as e:
            print(f"   ❌ Error: {e}")
        
        time.sleep(3)
    
    print(f"\n{'='*50}")
    print("Done! If all 3 show ✅ with filings, the retry script will work.")
    print("If you see ⚠️ HTML responses, BSE is throttling — try again later.")

if __name__ == "__main__":
    test()
