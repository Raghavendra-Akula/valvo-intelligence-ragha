"""
Email Service — Send comparison reports via SMTP
Configure SMTP_EMAIL, SMTP_PASSWORD, SMTP_HOST, SMTP_PORT in .env
"""

import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


def send_comparison_email(to_email, stocks_data, subject=None):
    """
    Send a formatted comparison report to the given email.
    
    Args:
        to_email: Recipient email address
        stocks_data: List of stock dicts (sorted by final_score desc)
        subject: Optional custom subject line
    
    Returns:
        dict with success status and message
    """
    
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))

    if not smtp_email or not smtp_password:
        return {
            "success": False,
            "message": "SMTP not configured. Add SMTP_EMAIL and SMTP_PASSWORD to your .env file."
        }

    # Build the report
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    if not subject:
        subject = f"VALVO Comparison Report — {now}"

    text_body = build_text_report(stocks_data, now)
    html_body = build_html_report(stocks_data, now)

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = smtp_email
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.send_message(msg)

        return {"success": True, "message": f"Report sent to {to_email}"}

    except smtplib.SMTPAuthenticationError:
        return {"success": False, "message": "SMTP authentication failed. Check your email/password or enable App Passwords."}
    except Exception as e:
        return {"success": False, "message": f"Email failed: {str(e)}"}


def build_text_report(stocks, timestamp):
    """Build a clean plain-text comparison report."""
    
    lines = []
    lines.append("=" * 64)
    lines.append("  VALVO STOCK COMPARISON REPORT")
    lines.append(f"  Generated: {timestamp}")
    lines.append(f"  Stocks Compared: {len(stocks)}")
    lines.append("=" * 64)
    lines.append("")

    # Summary table
    lines.append(f"{'#':<4} {'Stock':<20} {'Score':>6} {'Rating':<10} {'Sector':<25}")
    lines.append("-" * 64)

    for i, s in enumerate(stocks, 1):
        name = (s.get("stock_name") or "—")[:19]
        score = s.get("final_score", 0)
        rating = s.get("rating") or _get_rating(score)
        sector = (s.get("sector") or "—")[:24]
        lines.append(f"{i:<4} {name:<20} {score:>6.2f} {rating:<10} {sector:<25}")

    lines.append("")
    lines.append("-" * 64)
    lines.append("")

    # Detailed breakdown
    lines.append("DETAILED BREAKDOWN")
    lines.append("-" * 64)

    for i, s in enumerate(stocks, 1):
        score = s.get("final_score", 0)
        rating = s.get("rating") or _get_rating(score)
        lines.append("")
        lines.append(f"#{i}  {s.get('stock_name', '—')}  —  {score}/10  ({rating})")
        lines.append(f"    Sector: {s.get('sector', '—')}")
        lines.append(f"    Market Cap: ₹{_fmt(s.get('market_cap'))} Cr  |  Liquidity: ₹{_fmt(s.get('liquidity'))} Cr")
        lines.append(f"    ADR: {s.get('adr', '—')}%  |  Linearity: {s.get('linearity', '—')}")
        lines.append(f"    CMP: ₹{_fmt(s.get('market_price'))}")
        
        # Gatekeepers
        lines.append(f"    Gatekeepers → MCap: {_gk(s.get('market_cap'), 'mcap')} | Liq: {_gk(s.get('liquidity'), 'liq')} | Lin: {_gk(s.get('linearity'), 'lin')}")

    lines.append("")
    lines.append("=" * 64)
    lines.append("  VALVO · Volatility Volume Value")
    lines.append("=" * 64)

    return "\n".join(lines)


def build_html_report(stocks, timestamp):
    """Build an HTML version of the comparison report for email clients."""
    
    rows = ""
    for i, s in enumerate(stocks, 1):
        score = s.get("final_score", 0)
        rating = s.get("rating") or _get_rating(score)
        color = _rating_color(rating)
        rows += f"""
        <tr style="border-bottom:1px solid #1a1f2e;">
            <td style="padding:12px 8px;color:#8892a4;font-weight:700;">{i}</td>
            <td style="padding:12px 8px;font-weight:700;color:#e2e8f0;">{s.get('stock_name','—')}</td>
            <td style="padding:12px 8px;font-weight:800;color:{color};font-family:'Courier New',monospace;">{score:.2f}</td>
            <td style="padding:12px 8px;"><span style="background:{color}20;color:{color};padding:4px 10px;border-radius:6px;font-weight:700;font-size:12px;">{rating}</span></td>
            <td style="padding:12px 8px;color:#8892a4;">{s.get('sector','—')}</td>
            <td style="padding:12px 8px;color:#e2e8f0;font-family:'Courier New',monospace;">₹{_fmt(s.get('market_cap'))} Cr</td>
            <td style="padding:12px 8px;color:#e2e8f0;font-family:'Courier New',monospace;">₹{_fmt(s.get('liquidity'))} Cr</td>
            <td style="padding:12px 8px;color:#e2e8f0;">{s.get('adr','—')}%</td>
            <td style="padding:12px 8px;color:#e2e8f0;">{s.get('linearity','—')}</td>
        </tr>"""

    html = f"""
    <div style="background:#0d1117;color:#e2e8f0;font-family:'Segoe UI',Tahoma,Geneva,sans-serif;padding:32px;max-width:900px;margin:0 auto;">
        <div style="text-align:center;margin-bottom:28px;">
            <div style="display:inline-block;background:linear-gradient(135deg,#4299e1,#63b3ed);padding:8px 16px;border-radius:10px;font-weight:900;font-size:20px;color:#0d1117;letter-spacing:1px;">V</div>
            <h1 style="margin:12px 0 4px;font-size:24px;font-weight:800;">VALVO Comparison Report</h1>
            <p style="color:#8892a4;font-size:13px;">{timestamp} · {len(stocks)} stocks compared</p>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
                <tr style="border-bottom:2px solid #1a1f2e;">
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">#</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">STOCK</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">SCORE</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">RATING</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">SECTOR</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">MCAP</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">LIQUIDITY</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">ADR</th>
                    <th style="padding:10px 8px;text-align:left;color:#63b3ed;font-size:11px;letter-spacing:1px;">LINEARITY</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        <div style="text-align:center;margin-top:28px;padding-top:20px;border-top:1px solid #1a1f2e;">
            <p style="color:#4a5568;font-size:11px;letter-spacing:2px;font-weight:600;">VALVO · VOLATILITY VOLUME VALUE</p>
        </div>
    </div>
    """
    return html


def build_mailto_body(stocks):
    """Build a plain-text body suitable for mailto: links."""
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    return build_text_report(stocks, now)


# ── Helpers ──

def _fmt(val):
    if val is None or val == "":
        return "—"
    try:
        return f"{float(val):,.0f}"
    except (ValueError, TypeError):
        return str(val)


def _get_rating(score):
    if score >= 8: return "Excellent"
    if score >= 6: return "Strong"
    if score >= 4: return "Average"
    return "Weak"


def _rating_color(rating):
    return {
        "Excellent": "#00e676",
        "Strong": "#29b6f6",
        "Average": "#ffa726",
        "Weak": "#ef5350",
    }.get(rating, "#888")


def _gk(val, gk_type):
    """Calculate gatekeeper multiplier for display."""
    import math
    if gk_type == "mcap":
        if val is None or val == "": return "0.02"
        x = float(val)
        if x >= 1000: return "1.00"
        t = math.tanh((x - 950) / 40)
        return f"{min(0.02 + 0.98 * (t + 1) / 2, 1.0):.2f}"
    elif gk_type == "liq":
        if val is None or val == "": return "0.15"
        x = float(val)
        if x >= 200: return "1.00"
        t = math.tanh((x - 130) / 55)
        return f"{min(0.15 + 0.85 * (t + 1) / 2, 1.0):.2f}"
    elif gk_type == "lin":
        m = {"Very Good": "1.00", "Good": "0.85", "Bad": "0.15"}
        return m.get(val, "0.15")
    return "—"
