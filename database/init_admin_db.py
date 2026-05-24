"""
Admin Dashboard tables — user_subscriptions + admin_activity_log.
Called on app startup to ensure tables exist.
"""
from database.database import get_db, close_db


def init_admin_tables():
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id UUID NOT NULL UNIQUE,
                plan TEXT NOT NULL DEFAULT 'free',
                status TEXT NOT NULL DEFAULT 'active',
                billing_cycle TEXT DEFAULT 'monthly',
                start_date TIMESTAMPTZ DEFAULT NOW(),
                end_date TIMESTAMPTZ,
                amount NUMERIC(10,2) DEFAULT 0,
                payment_method TEXT,
                payment_ref TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_activity_log (
                id SERIAL PRIMARY KEY,
                admin_user_id UUID NOT NULL,
                action TEXT NOT NULL,
                target_user_id UUID,
                details JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_session_controls (
                user_id UUID PRIMARY KEY,
                force_logout_after TIMESTAMPTZ,
                reason TEXT,
                message TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pricing_plans (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                price_monthly NUMERIC(10,2) NOT NULL DEFAULT 0,
                price_yearly NUMERIC(10,2) NOT NULL DEFAULT 0,
                features JSONB DEFAULT '[]',
                is_active BOOLEAN DEFAULT true,
                sort_order INT DEFAULT 0,
                razorpay_plan_id_monthly TEXT,
                razorpay_plan_id_yearly TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS payment_events (
                id SERIAL PRIMARY KEY,
                user_id UUID NOT NULL,
                event_type TEXT NOT NULL,
                plan TEXT NOT NULL,
                amount NUMERIC(10,2) NOT NULL DEFAULT 0,
                currency TEXT DEFAULT 'INR',
                billing_cycle TEXT,
                razorpay_payment_id TEXT,
                razorpay_order_id TEXT,
                razorpay_subscription_id TEXT,
                status TEXT DEFAULT 'success',
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Extend pricing_plans with customization fields
        for col, defn in [
            ("trial_days", "INT DEFAULT 0"),
            ("badge", "TEXT"),
            ("description", "TEXT"),
            ("highlight", "BOOLEAN DEFAULT false"),
            ("billing_options", "JSONB DEFAULT '[]'"),
            ("cta_label", "TEXT"),
            ("icon", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS {col} {defn}")
            except Exception:
                pass

        try:
            cur.execute("ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS trial_used_at TIMESTAMPTZ")
        except Exception:
            pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS coupons (
                id SERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT,
                discount_type TEXT NOT NULL DEFAULT 'percent',
                discount_value NUMERIC(10,2) NOT NULL,
                max_uses INT,
                used_count INT DEFAULT 0,
                max_uses_per_user INT DEFAULT 1,
                applies_to JSONB DEFAULT '["pro","premium"]',
                billing_cycles JSONB DEFAULT '["monthly","quarterly","half_yearly","yearly"]',
                valid_from TIMESTAMPTZ DEFAULT NOW(),
                valid_until TIMESTAMPTZ,
                is_active BOOLEAN DEFAULT true,
                created_by UUID,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        try:
            cur.execute("""
                ALTER TABLE coupons
                ALTER COLUMN billing_cycles
                SET DEFAULT '["monthly","quarterly","half_yearly","yearly"]'::jsonb
            """)
        except Exception:
            pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS coupon_redemptions (
                id SERIAL PRIMARY KEY,
                coupon_id INT NOT NULL REFERENCES coupons(id),
                user_id UUID NOT NULL,
                plan TEXT NOT NULL,
                original_amount NUMERIC(10,2),
                discount_amount NUMERIC(10,2),
                final_amount NUMERIC(10,2),
                redeemed_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Seed default pricing plans (Basic / Pro / Premium)
        cur.execute("""
            INSERT INTO pricing_plans (name, display_name, price_monthly, price_yearly, features, sort_order, badge, highlight)
            VALUES
                ('basic', 'Basic', 499, 4999, '["Screener", "Watchlists", "Market Breadth", "IPO Zone"]', 0, 'Starter', false),
                ('pro', 'Pro', 999, 9999, '["Screener & Watchlists", "Sectoral Analysis", "Market Breadth", "IPO Zone", "Price Alerts", "Explore Stock", "Fundamentals"]', 1, 'Most Popular', true),
                ('premium', 'Premium', 2999, 29999, '["Everything in Pro", "Valvo Scoring Engine", "Position Manager", "Trade Journal", "Trade Analytics", "Valvo AI Assistant", "Past Winners", "Priority Support"]', 2, 'Full Access', false)
            ON CONFLICT (name) DO NOTHING
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Seed default user-visible sections
        cur.execute("""
            INSERT INTO app_config (key, value)
            VALUES ('user_visible_sections', '["screener","ipo-lab","watchlist","alerts","market-breadth","sectoral"]')
            ON CONFLICT (key) DO NOTHING
        """)

        cur.execute("""
            INSERT INTO app_config (key, value)
            VALUES (
                'paywall_config',
                '{
                    "headline":"Choose Your Plan",
                    "subheadline":"Pick the plan that fits your trading style",
                    "footer_note":"Secure payments via Razorpay. Cancel anytime.",
                    "default_billing_cycle":"monthly",
                    "trial_offer":{
                        "enabled":false,
                        "label":"Start Free Trial for 24 Hours",
                        "plan_name":"pro",
                        "billing_cycle":"monthly",
                        "duration_hours":24,
                        "badge":"Limited promotion"
                    }
                }'
            )
            ON CONFLICT (key) DO NOTHING
        """)

        cur.execute("""
            INSERT INTO app_config (key, value)
            VALUES (
                'session_security_config',
                '{
                    "enabled": true,
                    "max_devices": 3,
                    "logout_all_on_limit": true,
                    "alert_message": "Maximum devices reached. All active sessions were logged out for security. Please sign in again."
                }'
            )
            ON CONFLICT (key) DO NOTHING
        """)

        # Seed free subscriptions for any auth.users that don't have one yet
        cur.execute("""
            INSERT INTO user_subscriptions (user_id, plan, status)
            SELECT id, 'free', 'active'
            FROM auth.users
            WHERE id NOT IN (SELECT user_id FROM user_subscriptions)
        """)

        conn.commit()
        print("✅ Admin tables verified")
    except Exception as e:
        conn.rollback()
        print(f"⚠️ Admin tables init: {e}")
    finally:
        close_db(conn)
