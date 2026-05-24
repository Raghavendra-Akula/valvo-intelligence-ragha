"""
csv_upload_service.py — Parse Zerodha Console P&L CSVs and store as user_uploaded_trades.

Supports:
- Header-based parsing (auto-detects Zerodha column names)
- AI-assisted parsing via Gemini Flash for unusual CSV formats
- FY detection from trade dates
- Idempotent re-uploads via ON CONFLICT
"""
import csv
import io
import re
import os
import json
import uuid
from datetime import datetime

# ═══ Column name aliases — Zerodha changes these across years ═══
COLUMN_ALIASES = {
    "symbol": ["symbol", "instrument", "stock", "scrip", "trading_symbol", "tradingsymbol"],
    "isin": ["isin", "isin_code"],
    "buy_quantity": ["buy_quantity", "buy_qty", "buy qty", "buyqty"],
    "buy_value": ["buy_value", "buy_amount", "buy value", "buyvalue", "buy_avg"],
    "sell_quantity": ["sell_quantity", "sell_qty", "sell qty", "sellqty"],
    "sell_value": ["sell_value", "sell_amount", "sell value", "sellvalue", "sell_avg"],
    "realized_pl": [
        "realized_p&l", "realized p&l", "realised_p&l", "realised p&l",
        "realized_pnl", "realised_pnl", "pnl", "p&l", "p & l",
        "net_realized_profit", "net realized profit", "profit/loss",
    ],
    "trade_date": ["trade_date", "date", "trade date"],
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MONTH_LABELS = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def detect_fy(month, year):
    """April-March FY: April 2022 → '2022-23', March 2023 → '2022-23'."""
    if month >= 4:
        return f"{year}-{str(year + 1)[-2:]}"
    else:
        return f"{year - 1}-{str(year)[-2:]}"


def _normalize_header(raw):
    """Lowercase, strip whitespace for matching. Keep & and / for p&l matching."""
    return raw.strip().lower().replace("_", " ")


def _match_column(normalized_header, alias_key):
    """Check if a normalized header matches any alias for the given key."""
    h = normalized_header.strip()
    for alias in COLUMN_ALIASES.get(alias_key, []):
        a = alias.replace("_", " ")
        if h == a:
            return True
    return False


def _parse_number(val):
    """Parse Indian-format numbers: '1,23,456.78', '(500)', '-500', '₹500'."""
    if val is None:
        return 0.0
    s = str(val).strip()
    if not s or s == "-" or s.lower() == "nan":
        return 0.0
    # Remove currency symbols and whitespace
    s = re.sub(r"[₹$\s]", "", s)
    # Handle parenthesized negatives: (500) → -500
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    # Remove commas (Indian: 1,23,456 or Western: 123,456)
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _clean_symbol(raw):
    """Strip exchange suffixes: 'TATAMOTORS-EQ' → 'TATAMOTORS'."""
    s = str(raw).strip().upper()
    # Remove common suffixes
    for suffix in ["-EQ", "-BE", "-BZ", "-SM", "-ST", "-N1", "-N2", "-N3"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    # Remove exchange prefixes
    for prefix in ["NSE:", "BSE:", "NFO:", "BFO:"]:
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.strip()


def _detect_month_from_filename(filename):
    """Try to extract month/year from filename like 'pnl_april_2022.csv'."""
    name = filename.lower().replace("_", " ").replace("-", " ")
    for month_name, month_num in MONTH_NAMES.items():
        if month_name in name:
            # Find year near the month name
            years = re.findall(r"20\d{2}", name)
            if years:
                return month_num, int(years[0])
    return None, None


def _build_column_map(headers):
    """Map CSV header indices to canonical field names."""
    col_map = {}
    normalized = [_normalize_header(h) for h in headers]

    for idx, norm in enumerate(normalized):
        for key in COLUMN_ALIASES:
            if key not in col_map and _match_column(norm, key):
                col_map[key] = idx
                break

    return col_map


def _detect_encoding(raw_bytes):
    """Detect encoding — try UTF-8 first, then latin-1."""
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            raw_bytes.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def parse_zerodha_csv(file_bytes, filename="upload.csv"):
    """
    Parse a Zerodha Console P&L CSV.

    Returns:
        {
            "trades": [...],
            "month": int or None,
            "year": int or None,
            "fy": str or None,
            "warnings": [...],
            "errors": [...],
            "trade_count": int,
            "total_pl": float,
        }
    """
    warnings = []
    errors = []
    trades = []

    # Detect encoding
    encoding = _detect_encoding(file_bytes)
    text = file_bytes.decode(encoding)

    # Skip BOM and empty lines at start
    lines = text.strip().splitlines()
    if not lines:
        return {"trades": [], "errors": ["Empty file"], "warnings": [], "trade_count": 0, "total_pl": 0}

    # Find the header row — Zerodha sometimes has metadata rows before the actual CSV
    header_idx = 0
    for i, line in enumerate(lines[:10]):
        lower = line.lower()
        if any(alias in lower for aliases in COLUMN_ALIASES.values() for alias in aliases):
            header_idx = i
            break

    # Parse CSV from header row
    csv_text = "\n".join(lines[header_idx:])
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    if len(rows) < 2:
        return {"trades": [], "errors": ["No data rows found"], "warnings": [], "trade_count": 0, "total_pl": 0}

    headers = rows[0]
    col_map = _build_column_map(headers)

    # Validate required columns
    required = ["symbol", "realized_pl"]
    missing = [r for r in required if r not in col_map]
    if missing:
        return {
            "trades": [],
            "errors": [f"Missing required columns: {', '.join(missing)}. Found headers: {headers}"],
            "warnings": [],
            "trade_count": 0,
            "total_pl": 0,
            "needs_ai": True,
        }

    # Try to detect month/year from filename first
    file_month, file_year = _detect_month_from_filename(filename)

    detected_months = set()
    detected_years = set()

    for row_idx, row in enumerate(rows[1:], start=2):
        if not row or all(not cell.strip() for cell in row):
            continue  # skip empty rows

        try:
            symbol = _clean_symbol(row[col_map["symbol"]]) if "symbol" in col_map else ""
            if not symbol or symbol in ("TOTAL", "GRAND TOTAL", "NET"):
                continue  # skip summary rows

            buy_qty = _parse_number(row[col_map["buy_quantity"]]) if "buy_quantity" in col_map else 0
            sell_qty = _parse_number(row[col_map["sell_quantity"]]) if "sell_quantity" in col_map else 0
            quantity = max(buy_qty, sell_qty)

            buy_val = _parse_number(row[col_map["buy_value"]]) if "buy_value" in col_map else 0
            sell_val = _parse_number(row[col_map["sell_value"]]) if "sell_value" in col_map else 0
            realized_pl = _parse_number(row[col_map["realized_pl"]])

            # If buy/sell values look like per-share prices (not totals), multiply
            if buy_val > 0 and quantity > 0 and buy_val < quantity:
                buy_val = buy_val * quantity
            if sell_val > 0 and quantity > 0 and sell_val < quantity:
                sell_val = sell_val * quantity

            # Parse trade date if available
            trade_date = None
            if "trade_date" in col_map:
                date_str = row[col_map["trade_date"]].strip()
                for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y"]:
                    try:
                        trade_date = datetime.strptime(date_str, fmt).date()
                        detected_months.add(trade_date.month)
                        detected_years.add(trade_date.year)
                        break
                    except ValueError:
                        continue

            isin = row[col_map["isin"]].strip() if "isin" in col_map and col_map["isin"] < len(row) else None

            trade = {
                "symbol": symbol,
                "isin": isin,
                "quantity": quantity,
                "buy_value": round(buy_val, 2),
                "sell_value": round(sell_val, 2),
                "realized_pl": round(realized_pl, 2),
                "trade_date": str(trade_date) if trade_date else None,
            }
            trades.append(trade)

        except Exception as e:
            warnings.append(f"Row {row_idx}: {str(e)}")
            continue

    # Determine month and year
    month = file_month
    year = file_year

    if not month and detected_months:
        # Use most common month from trade dates
        month = max(set(detected_months), key=list(detected_months).count) if detected_months else None
    if not year and detected_years:
        year = max(set(detected_years), key=list(detected_years).count) if detected_years else None

    fy = detect_fy(month, year) if month and year else None

    # Add month/month_label to each trade
    for t in trades:
        if t.get("trade_date") and t["trade_date"] != "None":
            td = datetime.strptime(t["trade_date"], "%Y-%m-%d").date()
            t["month"] = td.month
            t["year"] = td.year
            t["month_label"] = f"{MONTH_LABELS[td.month]} {td.year}"
        elif month and year:
            t["month"] = month
            t["year"] = year
            t["month_label"] = f"{MONTH_LABELS[month]} {year}"
        else:
            warnings.append(f"Could not determine month for trade: {t['symbol']}")
            t["month"] = 1
            t["year"] = 2020
            t["month_label"] = "Unknown"

    total_pl = sum(t["realized_pl"] for t in trades)

    if not trades and not errors:
        errors.append("No valid trades found in file")

    return {
        "trades": trades,
        "month": month,
        "year": year,
        "fy": fy,
        "warnings": warnings,
        "errors": errors,
        "trade_count": len(trades),
        "total_pl": round(total_pl, 2),
    }


def compute_derived_fields(trades, base_capital):
    """Add realized_pl_pct, impact_on_pf, is_winner to each trade."""
    for t in trades:
        buy_val = t.get("buy_value", 0)
        t["realized_pl_pct"] = round((t["realized_pl"] / buy_val) * 100, 2) if buy_val > 0 else 0
        t["impact_on_pf"] = round((t["realized_pl"] / base_capital) * 100, 4) if base_capital > 0 else 0
        t["is_winner"] = t["realized_pl"] > 0
    return trades


def store_trades(conn, user_id, batch_id, trades, fy, base_capital):
    """
    Insert parsed trades into user_uploaded_trades.
    Uses ON CONFLICT to handle re-uploads.

    Returns: {"inserted": int, "updated": int, "skipped": int}
    """
    cur = conn.cursor()
    inserted = 0
    updated = 0

    for t in trades:
        # Compute derived fields per trade
        buy_val = t.get("buy_value", 0)
        realized_pl_pct = round((t["realized_pl"] / buy_val) * 100, 2) if buy_val > 0 else 0
        impact_on_pf = round((t["realized_pl"] / base_capital) * 100, 4) if base_capital > 0 else 0
        is_winner = t["realized_pl"] > 0

        cur.execute("""
            INSERT INTO user_uploaded_trades
                (user_id, fy, upload_batch_id, month, month_label, symbol, isin,
                 quantity, buy_value, sell_value, realized_pl, realized_pl_pct,
                 impact_on_pf, is_winner, trade_date, charges)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, fy, symbol, month, buy_value, sell_value)
            DO UPDATE SET
                realized_pl = EXCLUDED.realized_pl,
                realized_pl_pct = EXCLUDED.realized_pl_pct,
                impact_on_pf = EXCLUDED.impact_on_pf,
                is_winner = EXCLUDED.is_winner,
                quantity = EXCLUDED.quantity,
                upload_batch_id = EXCLUDED.upload_batch_id,
                trade_date = EXCLUDED.trade_date
        """, (
            user_id, fy, str(batch_id),
            t["month"], t["month_label"], t["symbol"], t.get("isin"),
            t.get("quantity", 0), t.get("buy_value", 0), t.get("sell_value", 0),
            t["realized_pl"], realized_pl_pct, impact_on_pf, is_winner,
            t.get("trade_date") if t.get("trade_date") != "None" else None,
            t.get("charges", 0),
        ))

        # xmax = 0 means fresh insert, > 0 means update
        if hasattr(cur, 'statusmessage') and 'UPDATE' in (cur.statusmessage or ''):
            updated += 1
        else:
            inserted += 1

    conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": 0}


def create_upload_record(conn, user_id, batch_id, fy, filename):
    """Create an audit record in user_csv_uploads."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_csv_uploads (user_id, batch_id, fy, filename, status)
        VALUES (%s, %s, %s, %s, 'processing')
        RETURNING id
    """, (user_id, str(batch_id), fy, filename))
    conn.commit()
    row = cur.fetchone()
    return str(row["id"])


def complete_upload_record(conn, upload_id, rows_parsed, rows_inserted, rows_skipped, errors, ai_tokens=0):
    """Mark upload as completed with stats."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE user_csv_uploads
        SET status = 'completed',
            rows_parsed = %s,
            rows_inserted = %s,
            rows_skipped = %s,
            errors = %s,
            ai_tokens_used = %s,
            completed_at = NOW()
        WHERE id = %s
    """, (rows_parsed, rows_inserted, rows_skipped, json.dumps(errors), ai_tokens, upload_id))
    conn.commit()


def fail_upload_record(conn, upload_id, errors):
    """Mark upload as failed."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE user_csv_uploads
        SET status = 'failed', errors = %s, completed_at = NOW()
        WHERE id = %s
    """, (json.dumps(errors), upload_id))
    conn.commit()


def ai_parse_csv(file_bytes, filename="upload.csv"):
    """
    Use Gemini Flash to parse a CSV that failed header-based detection.
    Sends first 30 rows to AI and asks for structured column mapping.

    Returns same format as parse_zerodha_csv.
    """
    encoding = _detect_encoding(file_bytes)
    text = file_bytes.decode(encoding)
    lines = text.strip().splitlines()

    # Take first 30 lines as sample
    sample = "\n".join(lines[:30])

    try:
        from google import genai
        from google.genai import types

        api_key = os.getenv("api_key", "").strip()
        if not api_key:
            return {
                "trades": [], "errors": ["AI parsing unavailable — no API key"],
                "warnings": [], "trade_count": 0, "total_pl": 0,
            }

        client = genai.Client(api_key=api_key, http_options={"timeout": 60})

        prompt = f"""You are a CSV parser. Analyze this Zerodha trading P&L CSV and extract trade data.

CSV sample (first 30 rows):
```
{sample}
```

Filename: {filename}

Return a JSON object with:
{{
  "column_map": {{
    "symbol": <0-based column index>,
    "buy_quantity": <index or null>,
    "buy_value": <index or null>,
    "sell_quantity": <index or null>,
    "sell_value": <index or null>,
    "realized_pl": <index>,
    "trade_date": <index or null>,
    "isin": <index or null>
  }},
  "header_row": <0-based row index of the header>,
  "data_start_row": <0-based row index where data begins>,
  "detected_month": <month number 1-12 or null>,
  "detected_year": <year like 2022 or null>
}}

Only return the JSON, no explanation."""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt],
            config=types.GenerateContentConfig(
                max_output_tokens=1000,
                temperature=0.1,
            ),
        )

        result_text = response.text.strip()
        # Extract JSON from response (may be wrapped in markdown code blocks)
        json_match = re.search(r"\{.*\}", result_text, re.DOTALL)
        if not json_match:
            return {
                "trades": [], "errors": ["AI could not parse the CSV format"],
                "warnings": [], "trade_count": 0, "total_pl": 0,
            }

        ai_result = json.loads(json_match.group())
        ai_col_map = ai_result.get("column_map", {})
        header_row = ai_result.get("header_row", 0)
        data_start = ai_result.get("data_start_row", header_row + 1)
        ai_month = ai_result.get("detected_month")
        ai_year = ai_result.get("detected_year")

        # Parse with AI-provided column map
        reader = csv.reader(io.StringIO(text))
        all_rows = list(reader)

        trades = []
        warnings = []

        for row_idx in range(data_start, len(all_rows)):
            row = all_rows[row_idx]
            if not row or all(not cell.strip() for cell in row):
                continue

            try:
                sym_idx = ai_col_map.get("symbol")
                if sym_idx is None:
                    continue
                symbol = _clean_symbol(row[sym_idx])
                if not symbol or symbol in ("TOTAL", "GRAND TOTAL", "NET"):
                    continue

                pl_idx = ai_col_map.get("realized_pl")
                realized_pl = _parse_number(row[pl_idx]) if pl_idx is not None else 0

                buy_qty = _parse_number(row[ai_col_map["buy_quantity"]]) if ai_col_map.get("buy_quantity") is not None else 0
                sell_qty = _parse_number(row[ai_col_map["sell_quantity"]]) if ai_col_map.get("sell_quantity") is not None else 0
                quantity = max(buy_qty, sell_qty)

                buy_val = _parse_number(row[ai_col_map["buy_value"]]) if ai_col_map.get("buy_value") is not None else 0
                sell_val = _parse_number(row[ai_col_map["sell_value"]]) if ai_col_map.get("sell_value") is not None else 0

                trade_date = None
                if ai_col_map.get("trade_date") is not None:
                    date_str = row[ai_col_map["trade_date"]].strip()
                    for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"]:
                        try:
                            trade_date = datetime.strptime(date_str, fmt).date()
                            break
                        except ValueError:
                            continue

                isin = row[ai_col_map["isin"]].strip() if ai_col_map.get("isin") is not None and ai_col_map["isin"] < len(row) else None

                month = trade_date.month if trade_date else (ai_month or 1)
                year = trade_date.year if trade_date else (ai_year or 2020)

                trades.append({
                    "symbol": symbol,
                    "isin": isin,
                    "quantity": quantity,
                    "buy_value": round(buy_val, 2),
                    "sell_value": round(sell_val, 2),
                    "realized_pl": round(realized_pl, 2),
                    "trade_date": str(trade_date) if trade_date else None,
                    "month": month,
                    "year": year,
                    "month_label": f"{MONTH_LABELS.get(month, 'Unknown')} {year}",
                })
            except Exception as e:
                warnings.append(f"AI-parsed row {row_idx}: {str(e)}")

        fy = detect_fy(ai_month or (trades[0]["month"] if trades else 1),
                       ai_year or (trades[0]["year"] if trades else 2020)) if trades else None

        tokens_used = getattr(response, 'usage_metadata', None)
        token_count = 0
        if tokens_used:
            token_count = getattr(tokens_used, 'total_token_count', 0) or 0

        return {
            "trades": trades,
            "month": ai_month,
            "year": ai_year,
            "fy": fy,
            "warnings": warnings,
            "errors": [],
            "trade_count": len(trades),
            "total_pl": round(sum(t["realized_pl"] for t in trades), 2),
            "ai_assisted": True,
            "ai_tokens_used": token_count,
        }

    except Exception as e:
        return {
            "trades": [], "errors": [f"AI parsing failed: {str(e)}"],
            "warnings": [], "trade_count": 0, "total_pl": 0,
        }


def get_upload_status(conn, user_id):
    """Get all uploads for a user, grouped by FY."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id, batch_id, fy, filename, status, rows_parsed, rows_inserted,
               rows_skipped, errors, ai_tokens_used, created_at, completed_at
        FROM user_csv_uploads
        WHERE user_id = %s
        ORDER BY fy, created_at DESC
    """, (user_id,))
    rows = cur.fetchall()

    by_fy = {}
    for r in rows:
        fy = r["fy"]
        if fy not in by_fy:
            by_fy[fy] = []
        by_fy[fy].append({
            "id": str(r["id"]),
            "filename": r["filename"],
            "status": r["status"],
            "rows_parsed": r["rows_parsed"],
            "rows_inserted": r["rows_inserted"],
            "rows_skipped": r["rows_skipped"],
            "ai_tokens_used": r["ai_tokens_used"],
            "created_at": str(r["created_at"]),
        })

    return by_fy


def delete_fy_uploads(conn, user_id, fy):
    """Delete all uploaded trades and upload records for a user+FY."""
    cur = conn.cursor()
    cur.execute("DELETE FROM user_uploaded_trades WHERE user_id = %s AND fy = %s", (user_id, fy))
    trades_deleted = cur.rowcount
    cur.execute("DELETE FROM user_csv_uploads WHERE user_id = %s AND fy = %s", (user_id, fy))
    cur.execute("DELETE FROM user_fy_config WHERE user_id = %s AND fy = %s", (user_id, fy))
    conn.commit()
    return trades_deleted
