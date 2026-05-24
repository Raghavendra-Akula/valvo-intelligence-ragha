import os
import re
from flask import Flask
from flask_cors import CORS

# ═══════════════════════════════════════════════════════════
# Sentry — auto-report unhandled backend exceptions in real time.
# No-op when SENTRY_DSN is unset (local dev / self-hosted).
# ═══════════════════════════════════════════════════════════
_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            release=os.getenv("SENTRY_RELEASE", "valvo-backend"),
            send_default_pii=False,
        )
    except Exception as _exc:
        print(f"[sentry] init failed (non-fatal): {_exc}")

from routes.score_routes import score_bp
from routes.history_routes import history_bp
from routes.admin_routes import admin_bp
from routes.auth_routes import auth_bp
from routes.compare_routes import compare_bp
from routes.position_routes import position_bp
from routes.chat_routes import chat_bp
from routes.screener_routes import screener_bp
from routes.valvo_journal_routes import valvo_journal_bp
from routes.analytics_routes import analytics_bp
from routes.voice_routes import voice_bp
from routes.valvo_ai_v7_routes import valvo_ai_v7_bp
from routes.ipo_routes import ipo_bp
from routes.market_holidays_routes import market_holidays_bp
from routes.dashboard_insights_routes import dashboard_insights_bp
from routes.sector_routes import sector_bp
from routes.explore_routes import explore_bp

from routes.journal_routes import journal_bp
from routes.alerts_routes import alerts_bp
from routes.settings_routes import settings_bp
from routes.ma_engine_routes import ma_engine_bp
from routes.breadth_routes import breadth_bp
from routes.breakout_routes import breakout_bp
from routes.past_winners_routes import past_winners_bp
from routes.deep_research_routes import deep_research_bp
from routes.user_routes import user_bp
from routes.project_hub_routes import project_hub_bp
from routes.fundamentals_routes import fundamentals_bp
from routes.admin_dashboard_routes import admin_dashboard_bp
from routes.drawings_routes import drawings_bp
from routes.pw_drawings_routes import pw_drawings_bp
from routes.feature_routes import feature_bp
from routes.csv_upload_routes import csv_upload_bp
from routes.custom_sectors_routes import custom_sectors_bp
from routes.themes_routes import themes_bp
from routes.classification_v2_routes import classification_v2_bp
from routes.theme_explanation_routes import theme_explanation_bp
from routes.sector_thesis_routes import sector_thesis_bp
from routes.catalysts_routes import catalysts_bp
from routes.explore_extras_routes import explore_extras_bp
from routes.earnings_tracker_routes import earnings_tracker_bp
from routes.health_routes import health_bp
from routes.rationale_routes import rationale_bp
from routes.coach_routes import coach_bp
from routes.dhan_routes import dhan_bp
from services.session_security_service import SessionAccessDenied, enforce_session_access

app = Flask(__name__)
ALLOWED_ORIGINS = [
    o.strip() for o in
    re.split(r"[,\n]+", os.getenv(
        "ALLOWED_ORIGINS",
        "https://app.valvointelligence.com,https://valvointelligence.com,"
        "http://localhost:5173,http://localhost:5174,"
        "http://127.0.0.1:5173,http://127.0.0.1:5174,"
        "http://localhost:5001,http://127.0.0.1:5001"
    ))
    if o.strip()
]
# Allow Vercel preview deployments via regex pattern (Flask-CORS supports this)
ALLOWED_ORIGINS.append(r"https://valvo-intelligence-final.*\.vercel\.app")
CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)

from extensions import limiter
limiter.init_app(app)

# ═══════════════════════════════════════════════════════════
# AUTH MIDDLEWARE — validates Supabase JWT on ALL endpoints
# Uses JWKS (asymmetric ES256) with HS256 legacy fallback
# ═══════════════════════════════════════════════════════════
from flask import g, jsonify as _jsonify

AUTH_EXEMPT_ENDPOINTS = {
    "auth.verify_token",  # Token check itself doesn't need auth
    "project_hub.run_reminders",  # Cron job — uses X-Cron-Secret instead
    "feature.run_cleanup",        # Cron job — uses X-Cron-Secret instead
    "position.write_portfolio_snapshot",  # Cron job — uses X-Cron-Secret instead
    "position.write_earnings_reminders",  # Cron job — uses X-Cron-Secret instead
    "position.post_result_refresh",       # Cron job — uses X-Cron-Secret instead
    "breadth.run_finalise",               # Cron job — uses X-Cron-Secret instead
    "admin_dashboard.payment_webhook",  # Razorpay webhook — uses signature
    "user.get_pricing",   # Pricing page — shown before user has access
    "home",               # Health check / root
    "static",             # Flask static files
    "health.data_freshness",  # Operational health probe — for uptime monitors
    "health.llm_heartbeat",   # LLM uptime probe — pings Gemini + Kimi
}

@app.before_request
def require_auth():
    """Validate Supabase JWT on every request (except exempt endpoints).

    Two auth paths are accepted:
      1. `Authorization: Bearer <supabase-jwt>` — the normal user path.
      2. `X-Admin-Token: vk_…`                   — long-lived admin API
         key for headless workers (research-worker, CI scripts, etc.).
         See Backend/services/admin_api_keys.py.
    """
    from flask import request as req
    from routes.auth_routes import _verify_supabase_jwt
    import jwt as pyjwt

    # Skip auth for exempt endpoints and CORS preflight
    if req.endpoint in AUTH_EXEMPT_ENDPOINTS:
        return None
    if req.method == "OPTIONS":
        return None

    # ── Path 2: admin API key header ─────────────────────────────────
    admin_token = req.headers.get("X-Admin-Token", "").strip()
    if admin_token:
        try:
            from services.admin_api_keys import validate_key
            owner_id = validate_key(admin_token)
        except Exception as exc:
            print(f"AUTH_API_KEY_LOOKUP_ERROR endpoint={req.endpoint} type={type(exc).__name__} detail={exc}")
            return _jsonify({"error": "API key validation failed"}), 401
        if owner_id:
            g.user_id = owner_id
            g.user_email = ""
            g.user_session_id = None
            g.session_security = None
            g.auth_via = "admin_api_key"
            return None
        return _jsonify({"error": "Invalid or revoked admin API key"}), 401

    # ── Path 1: Supabase JWT (existing) ──────────────────────────────
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _jsonify({"error": "Login required"}), 401

    token = auth_header.split(" ")[1]
    try:
        payload = _verify_supabase_jwt(token)
        g.user_id = payload.get("sub")
        g.user_email = payload.get("email", "")
        g.user_session_id = payload.get("session_id")
        g.session_security = enforce_session_access(g.user_id, payload)
        g.auth_via = "supabase_jwt"
    except pyjwt.ExpiredSignatureError:
        return _jsonify({"error": "Session expired"}), 401
    except SessionAccessDenied as exc:
        return _jsonify({"error": exc.message, "code": exc.code}), 401
    except Exception as exc:
        print(f"AUTH_MIDDLEWARE_ERROR endpoint={req.endpoint} type={type(exc).__name__} detail={exc}")
        return _jsonify({"error": "Invalid token"}), 401

# Register all blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(score_bp)
app.register_blueprint(history_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(compare_bp)
app.register_blueprint(position_bp)
app.register_blueprint(screener_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(valvo_journal_bp)
app.register_blueprint(analytics_bp)
app.register_blueprint(voice_bp)
app.register_blueprint(valvo_ai_v7_bp)
app.register_blueprint(ipo_bp)
app.register_blueprint(market_holidays_bp)
app.register_blueprint(dashboard_insights_bp)
app.register_blueprint(sector_bp)
app.register_blueprint(explore_bp)

app.register_blueprint(journal_bp)
app.register_blueprint(alerts_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(ma_engine_bp)
app.register_blueprint(breadth_bp)
app.register_blueprint(breakout_bp)
app.register_blueprint(past_winners_bp)
app.register_blueprint(deep_research_bp)
app.register_blueprint(user_bp)
app.register_blueprint(project_hub_bp)
app.register_blueprint(fundamentals_bp)
app.register_blueprint(admin_dashboard_bp)
app.register_blueprint(drawings_bp)
app.register_blueprint(pw_drawings_bp)
app.register_blueprint(feature_bp)
app.register_blueprint(csv_upload_bp)
app.register_blueprint(custom_sectors_bp)
app.register_blueprint(themes_bp)
app.register_blueprint(classification_v2_bp)
app.register_blueprint(theme_explanation_bp)
app.register_blueprint(sector_thesis_bp)
app.register_blueprint(catalysts_bp)
app.register_blueprint(explore_extras_bp)
app.register_blueprint(earnings_tracker_bp)
app.register_blueprint(health_bp)
app.register_blueprint(rationale_bp)
app.register_blueprint(coach_bp)
app.register_blueprint(dhan_bp)


@app.route("/api/<path:_path>", methods=["OPTIONS"])
def api_preflight(_path):
    """Explicit API preflight handler for Cloud Run/browser CORS requests."""
    return ("", 204)


# Auto-create position table on startup
try:
    from database.init_positions_db import init_positions_table
    init_positions_table()
except Exception as e:
    print(f"⚠️ Position table init skipped: {e}")

try:
    from database.init_watchlist_db import init_multi_watchlist_tables
    init_multi_watchlist_tables()
except Exception as e:
    print(f"⚠️ Watchlist table init skipped: {e}")

# Auto-create valvo journal tables on startup
try:
    from database.migrate_valvo_journal import migrate as migrate_valvo_journal
    migrate_valvo_journal()
except Exception as e:
    print(f"⚠️ Valvo Journal table init skipped: {e}")

try:
    from database.init_chat_db import init_chat_table
    init_chat_table()
    from database.init_deep_research_db import init_deep_research_table
    init_deep_research_table()
except Exception as e:
    print(f"⚠️ Chat table init skipped: {e}")

try:
    from database.init_valvo_ai_v2_db import init_valvo_ai_v2_tables
    init_valvo_ai_v2_tables()
except Exception as e:
    print(f"⚠️ Valvo AI v2 table init skipped: {e}")

try:
    from database.init_admin_db import init_admin_tables
    init_admin_tables()
except Exception as e:
    print(f"⚠️ Admin tables init skipped: {e}")

try:
    from database.init_dhan_db import init_dhan_tables
    init_dhan_tables()
except Exception as e:
    print(f"⚠️ Dhan tables init skipped: {e}")

# Auto-create market_regime_history + leading_sectors tables
# NOTE: user_settings is now multi-tenant (user_id UUID PK) — managed by settings_routes.py
# Do NOT create or seed user_settings here — it conflicts with the per-user schema
try:
    from database.database import get_db, close_db
    _conn = get_db()
    if _conn:
        _cur = _conn.cursor()
        _cur.execute("""
            CREATE TABLE IF NOT EXISTS market_regime_history (
                id SERIAL PRIMARY KEY,
                regime TEXT NOT NULL DEFAULT 'bull',
                note TEXT DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        _cur.execute("""
            CREATE TABLE IF NOT EXISTS leading_sectors (
                id SERIAL PRIMARY KEY,
                sectors JSONB DEFAULT '[]',
                regime TEXT DEFAULT 'bull',
                note TEXT DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        _conn.commit()
        close_db(_conn)
        print("✅ Shared tables verified")
except Exception as e:
    print(f"⚠️ Shared tables init skipped: {e}")

# Clean up fake holiday candles (WebSocket VM inserts duplicates on NSE holidays)
try:
    from config.settings import NSE_HOLIDAYS
    from database.database import get_db, close_db
    _hconn = get_db()
    if _hconn:
        _hcur = _hconn.cursor()
        holidays = list(NSE_HOLIDAYS)
        _hcur.execute("DELETE FROM candles_daily WHERE date = ANY(%s::date[])", (holidays,))
        d1 = _hcur.rowcount
        _hcur.execute("DELETE FROM candles_indices WHERE date = ANY(%s::date[])", (holidays,))
        d2 = _hcur.rowcount
        _hconn.commit()
        close_db(_hconn)
        if d1 or d2:
            print(f"🧹 Holiday cleanup: removed {d1} daily + {d2} index candles on NSE holidays")
        else:
            print("✅ No holiday candles to clean")
except Exception as e:
    print(f"⚠️ Holiday cleanup skipped: {e}")

# Auto-create feature requests tables on startup
try:
    from database.init_feature_requests_db import init_feature_requests_tables
    init_feature_requests_tables()
except Exception as e:
    print(f"⚠️ Feature requests tables init skipped: {e}")

@app.route("/")
def home():
    return {
        "message": "Stock Scoring API is running",
        "endpoints": {
            "login": "/api/login",
            "verify": "/api/verify-token",
            "score": "/api/calculate-score",
            "submissions": "/api/submissions",
            "settings": "/api/settings",
        }
    }

if __name__ == "__main__":
    app.run(debug=True)

# Deploy command: see DEPLOY.md for full instructions with env vars
