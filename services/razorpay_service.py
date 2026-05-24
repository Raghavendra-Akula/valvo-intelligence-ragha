"""
Razorpay Payment Gateway Service — Subscription-based recurring billing.
Handles plan creation, subscription management, payment verification, and webhooks.
Keys stored in DB (app_config table) so admin can configure from dashboard.
Env vars RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET used as fallback.
"""
import os, json, hmac, hashlib
from database.database import get_db, close_db
from services.admin_service import normalize_billing_options

_client = None
_cached_keys = None


def _get_keys():
    """Get Razorpay keys — DB first (app_config), env vars as fallback."""
    global _cached_keys
    if _cached_keys:
        return _cached_keys

    # Try DB first
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_config WHERE key = 'razorpay_config'")
        row = cur.fetchone()
        if row and row["value"]:
            cfg = row["value"] if isinstance(row["value"], dict) else json.loads(row["value"])
            key_id = cfg.get("key_id", "")
            key_secret = cfg.get("key_secret", "")
            webhook_secret = cfg.get("webhook_secret", "")
            mode = cfg.get("mode", "test")
            if key_id and key_secret:
                _cached_keys = {"key_id": key_id, "key_secret": key_secret, "webhook_secret": webhook_secret, "mode": mode}
                return _cached_keys
    except Exception:
        pass
    finally:
        close_db(conn)

    # Fallback to env vars
    key_id = os.getenv("RAZORPAY_KEY_ID", "")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if key_id and key_secret:
        mode = "live" if key_id.startswith("rzp_live_") else "test"
        _cached_keys = {"key_id": key_id, "key_secret": key_secret, "webhook_secret": webhook_secret, "mode": mode}
    return _cached_keys


def _get_client():
    global _client
    keys = _get_keys()
    if not keys:
        raise Exception("Razorpay not configured. Add keys in Admin → Pricing or set RAZORPAY_KEY_ID env var.")
    import razorpay
    _client = razorpay.Client(auth=(keys["key_id"], keys["key_secret"]))
    return _client


def invalidate_cache():
    """Call after saving new keys to DB."""
    global _client, _cached_keys
    _client = None
    _cached_keys = None


def is_configured():
    keys = _get_keys()
    return bool(keys and keys.get("key_id"))


def get_config_status():
    """Return safe config info (no secrets) for admin dashboard."""
    keys = _get_keys()
    if not keys:
        return {"configured": False, "mode": None, "key_id_preview": None}
    kid = keys["key_id"]
    return {
        "configured": True,
        "mode": keys.get("mode", "test"),
        "key_id_preview": kid[:12] + "..." + kid[-4:] if len(kid) > 16 else kid,
        "has_webhook_secret": bool(keys.get("webhook_secret")),
    }


def save_config(key_id, key_secret, webhook_secret=""):
    """Save Razorpay keys to app_config table."""
    mode = "live" if key_id.startswith("rzp_live_") else "test"
    config = {
        "key_id": key_id.strip(),
        "key_secret": key_secret.strip(),
        "webhook_secret": webhook_secret.strip(),
        "mode": mode,
    }
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_config (key, value, updated_at)
            VALUES ('razorpay_config', %s::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (json.dumps(config),))
        conn.commit()
        invalidate_cache()
        return {"saved": True, "mode": mode}
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


# ── Subscription-based Plans ─────────────────────────────────


def create_razorpay_plan(name, amount_paise, period="monthly", interval=1, description=""):
    """Create a recurring plan in Razorpay. Returns plan_id."""
    client = _get_client()
    plan = client.plan.create({
        "period": period,
        "interval": interval,
        "item": {
            "name": name,
            "amount": int(amount_paise),
            "currency": "INR",
            "description": description or name,
        },
    })
    return plan["id"]


def _razorpay_period_for_months(months):
    months = max(1, int(months or 1))
    if months % 12 == 0:
        return "yearly", max(1, months // 12)
    return "monthly", months


def sync_plans_to_razorpay():
    """Create Razorpay plans for each pricing_plan that doesn't have one yet."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pricing_plans WHERE is_active = true AND name != 'free' ORDER BY sort_order")
        plans = cur.fetchall()

        results = []
        for p in plans:
            billing_options = normalize_billing_options(p.get("billing_options"), p.get("price_monthly"), p.get("price_yearly"))
            synced = False
            for option in billing_options:
                if not option.get("enabled") or float(option.get("price") or 0) <= 0 or option.get("razorpay_plan_id"):
                    continue
                period, interval = _razorpay_period_for_months(option.get("months"))
                rz_id = create_razorpay_plan(
                    f"{p['display_name']} — {option['label']}",
                    int(float(option["price"]) * 100),
                    period=period,
                    interval=interval,
                    description=f"Valvo {p['display_name']} {option['label']}",
                )
                option["razorpay_plan_id"] = rz_id
                synced = True
                results.append({"plan": p["name"], "cycle": option["id"], "razorpay_id": rz_id})

            monthly_legacy = next((item.get("razorpay_plan_id") for item in billing_options if int(item.get("months") or 0) == 1), None)
            yearly_legacy = next((item.get("razorpay_plan_id") for item in billing_options if int(item.get("months") or 0) == 12), None)
            cur.execute("""
                UPDATE pricing_plans
                SET billing_options = %s::jsonb,
                    razorpay_plan_id_monthly = %s,
                    razorpay_plan_id_yearly = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (json.dumps(billing_options), monthly_legacy, yearly_legacy, p["id"]))

            if not synced:
                results.append({"plan": p["name"], "status": "already_synced"})

        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


# ── Subscriptions ─────────────────────────────────────────────


def create_subscription(user_id, user_email, plan_name, billing_cycle="monthly"):
    """Create a Razorpay subscription for a user. Returns subscription_id + key_id for frontend checkout."""
    client = _get_client()
    keys = _get_keys()

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pricing_plans WHERE name = %s", (plan_name,))
        row = cur.fetchone()
        if not row:
            raise Exception(f"Plan not found: {plan_name}")

        options = normalize_billing_options(row.get("billing_options"), row.get("price_monthly"), row.get("price_yearly"))
        selected = next((item for item in options if item["id"] == billing_cycle and item.get("enabled")), None)
        if not selected:
            selected = next((item for item in options if item["id"] == billing_cycle), None)
        if not selected or not selected.get("razorpay_plan_id"):
            raise Exception(f"Razorpay plan not synced for {plan_name}/{billing_cycle}. Sync plans first.")

        months = max(1, int(selected.get("months") or 1))
        total_count = max(1, 60 // months)  # Keep subscriptions renewable for ~5 years
        sub = client.subscription.create({
            "plan_id": selected["razorpay_plan_id"],
            "total_count": total_count,
            "customer_notify": 1,
            "notes": {"user_id": str(user_id), "email": user_email, "plan": plan_name},
        })

        amount = float(selected.get("price") or 0)
        cur.execute("""
            INSERT INTO payment_events (user_id, event_type, plan, amount, billing_cycle, razorpay_subscription_id, status)
            VALUES (%s, 'subscription_created', %s, %s, %s, %s, 'pending')
        """, (user_id, plan_name, amount, billing_cycle, sub["id"]))
        conn.commit()

        return {
            "subscription_id": sub["id"],
            "key_id": keys["key_id"],
            "plan": plan_name,
            "billing_cycle": billing_cycle,
            "amount": amount,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def verify_subscription_payment(payment_id, subscription_id, signature):
    """Verify Razorpay subscription payment signature."""
    client = _get_client()
    client.utility.verify_payment_signature({
        "razorpay_payment_id": payment_id,
        "razorpay_subscription_id": subscription_id,
        "razorpay_signature": signature,
    })

    # Activate subscription in our DB
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, plan, amount, billing_cycle FROM payment_events
            WHERE razorpay_subscription_id = %s AND event_type = 'subscription_created'
            ORDER BY created_at DESC LIMIT 1
        """, (subscription_id,))
        row = cur.fetchone()
        if row:
            cur.execute("""
                UPDATE user_subscriptions
                SET plan = %s, status = 'active', billing_cycle = %s,
                    amount = %s, payment_method = 'razorpay',
                    payment_ref = %s, start_date = NOW(), updated_at = NOW()
                WHERE user_id = %s
            """, (row["plan"], row["billing_cycle"], row["amount"], subscription_id, row["user_id"]))

            cur.execute("""
                INSERT INTO payment_events (user_id, event_type, plan, amount, billing_cycle, razorpay_payment_id, razorpay_subscription_id, status)
                VALUES (%s, 'payment', %s, %s, %s, %s, %s, 'success')
            """, (row["user_id"], row["plan"], row["amount"], row["billing_cycle"], payment_id, subscription_id))

        conn.commit()
        return {"verified": True, "user_id": str(row["user_id"]) if row else None}
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


# ── Webhooks ──────────────────────────────────────────────────


def handle_webhook(payload, signature):
    """Process Razorpay subscription webhook events."""
    keys = _get_keys()
    if keys and keys.get("webhook_secret"):
        expected = hmac.new(
            keys["webhook_secret"].encode(),
            payload.encode() if isinstance(payload, str) else payload,
            hashlib.sha256,
        ).hexdigest()
        if expected != signature:
            raise Exception("Webhook signature invalid")

    data = json.loads(payload) if isinstance(payload, str) else payload
    event = data.get("event", "")
    entity = data.get("payload", {}).get("subscription", {}).get("entity", {})
    payment_entity = data.get("payload", {}).get("payment", {}).get("entity", {})

    conn = get_db()
    try:
        cur = conn.cursor()
        sub_id = entity.get("id") or payment_entity.get("subscription_id")
        notes = entity.get("notes", {})
        user_id = notes.get("user_id")
        plan = notes.get("plan")

        if event == "subscription.activated":
            if user_id:
                cur.execute("""
                    UPDATE user_subscriptions SET status = 'active', updated_at = NOW()
                    WHERE user_id = %s::uuid
                """, (user_id,))

        elif event == "subscription.charged":
            if user_id and plan:
                amount = float(payment_entity.get("amount", 0)) / 100
                cur.execute("""
                    INSERT INTO payment_events (user_id, event_type, plan, amount, razorpay_payment_id, razorpay_subscription_id, status)
                    VALUES (%s, 'payment', %s, %s, %s, %s, 'success')
                """, (user_id, plan, amount, payment_entity.get("id"), sub_id))

        elif event == "subscription.cancelled":
            if user_id:
                cur.execute("""
                    UPDATE user_subscriptions SET status = 'cancelled', updated_at = NOW()
                    WHERE user_id = %s::uuid
                """, (user_id,))
                cur.execute("""
                    INSERT INTO payment_events (user_id, event_type, plan, amount, razorpay_subscription_id, status)
                    VALUES (%s, 'subscription_cancel', %s, 0, %s, 'success')
                """, (user_id, plan or 'unknown', sub_id))

        elif event == "subscription.halted":
            if user_id:
                cur.execute("""
                    UPDATE user_subscriptions SET status = 'expired', updated_at = NOW()
                    WHERE user_id = %s::uuid
                """, (user_id,))

        elif event in ("payment.captured", "payment.failed"):
            order_id = payment_entity.get("order_id")
            status = "success" if event == "payment.captured" else "failed"
            if order_id:
                cur.execute("""
                    UPDATE payment_events SET status = %s, razorpay_payment_id = %s
                    WHERE razorpay_order_id = %s AND status = 'pending'
                """, (status, payment_entity.get("id"), order_id))

        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        close_db(conn)

    return {"processed": True, "event": event}
