"""
user_analytics_service.py — Per-user FY resolution for Trade Analytics.

Resolves which FYs a user can see, what table to query, what base capital to use,
and whether user_id filtering is needed.

SECURITY NOTE: all trade tables (journal_trades, legacy_trades*) have a user_id
UUID column AND an RLS policy. The Python backend connects as service_role which
BYPASSES RLS, so every query must have an explicit `WHERE user_id = %s`.
"user_filter" is therefore ALWAYS True for these tables below — earlier
single-tenant wording in this file described an incorrect assumption that
legacy tables were admin-only / lacked user_id. They were, then got the
column added, and the flag here wasn't updated until a cross-user leak
surfaced it. Keep this file honest.
"""

# ═══ Legacy FY table mapping (all have user_id column) ═══
LEGACY_FY_TABLES = {
    "2020-21": "legacy_trades_fy2021",
    "2021-22": "legacy_trades_fy2122",
    "2022-23": "legacy_trades_fy2223",
    "2023-24": "legacy_trades_fy2324",
    "2024-25": "legacy_trades_fy2425",
    "2025-26": "legacy_trades",
}

# FYs that use journal_trades_computed (has user_id)
JOURNAL_FY_TABLE = "journal_trades_computed"

# User-uploaded CSV trades (has user_id + fy columns)
UPLOADED_TRADES_TABLE = "user_uploaded_trades"


def get_user_role(cur, user_id):
    """Returns 'admin' or 'user'. Defaults to 'user' if no profile exists."""
    cur.execute("SELECT role FROM user_profiles WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    return row["role"] if row else "user"


def get_user_fy_list(cur, user_id):
    """Returns list of FY strings available to this user, plus 'all'.
    Visibility is driven by user_fy_config — you see a FY only if you
    have a config entry for it. Legacy FYs require a config entry too."""
    # Get FYs this user has configured base capital for
    cur.execute(
        "SELECT fy FROM user_fy_config WHERE user_id = %s ORDER BY fy",
        (user_id,)
    )
    configured_fys = [r["fy"] for r in cur.fetchall()]

    result = sorted(configured_fys)
    if len(result) > 1:
        result.append("all")
    return result


def get_user_base_capital(cur, user_id, fy):
    """Returns base capital for a user+FY combo. None if not configured."""
    cur.execute(
        "SELECT base_capital FROM user_fy_config WHERE user_id = %s AND fy = %s",
        (user_id, fy)
    )
    row = cur.fetchone()
    return float(row["base_capital"]) if row else None


def set_user_base_capital(cur, user_id, fy, base_capital):
    """Upsert base capital for a user+FY. The source of truth for base capital
    going forward — user_settings.base_capital and journal_settings.portfolio_capital
    are deprecated and no longer written.

    Returns the saved value as float.
    """
    cur.execute(
        """
        INSERT INTO user_fy_config (user_id, fy, base_capital, created_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id, fy) DO UPDATE SET base_capital = EXCLUDED.base_capital
        RETURNING base_capital
        """,
        (str(user_id), fy, float(base_capital)),
    )
    row = cur.fetchone()
    return float(row["base_capital"]) if row else float(base_capital)


def _has_uploaded_trades(cur, user_id, fy):
    """Check if user has uploaded CSV trades for a specific FY."""
    cur.execute(
        "SELECT 1 FROM user_uploaded_trades WHERE user_id = %s AND fy = %s LIMIT 1",
        (user_id, fy)
    )
    return cur.fetchone() is not None


def resolve_fy(cur, user_id, fy):
    """
    Resolve an FY selection into query parameters.

    Returns dict:
        table:      SQL table/subquery string
        base:       base capital in ₹
        user_filter: True if WHERE user_id = %s should be appended
        fy_filter:  FY string if table needs fy column filtering (uploaded trades)
        allowed:    True if this user can see this FY
        role:       'admin' or 'user'
        source:     'legacy', 'journal', or 'uploaded'

    For fy='all', builds the appropriate UNION query.
    """
    role = get_user_role(cur, user_id)

    if fy == "all":
        return _resolve_all(cur, user_id, role)

    # Check for user-uploaded CSV data first (works for ANY FY)
    if _has_uploaded_trades(cur, user_id, fy):
        base = get_user_base_capital(cur, user_id, fy)
        if base is None:
            return {"allowed": False, "needs_setup": True, "role": role}
        return {
            "table": UPLOADED_TRADES_TABLE,
            "base": base,
            "user_filter": True,
            "fy_filter": fy,
            "allowed": True,
            "role": role,
            "source": "uploaded",
        }

    # Legacy FY — has user_id column (see module docstring), MUST filter.
    if fy in LEGACY_FY_TABLES:
        base = get_user_base_capital(cur, user_id, fy)
        if base is None:
            return {"allowed": False, "role": role}
        return {
            "table": LEGACY_FY_TABLES[fy],
            "base": base,
            "user_filter": True,
            "allowed": True,
            "role": role,
            "source": "legacy",
        }

    # Journal FY (2026-27+)
    base = get_user_base_capital(cur, user_id, fy)
    if base is None:
        return {"allowed": False, "needs_setup": True, "role": role}

    return {
        "table": JOURNAL_FY_TABLE,
        "base": base,
        "user_filter": True,
        "allowed": True,
        "role": role,
        "source": "journal",
    }


def _resolve_all(cur, user_id, role):
    """Build the UNION query for 'all' FYs visible to this user."""
    cols = "id, month, month_label, symbol, quantity, buy_value, sell_value, " \
           "realized_pl, realized_pl_pct, impact_on_pf, is_winner"
    parts = []
    earliest_base = None
    uploaded_fys_included = set()

    # 1. Check for user-uploaded FYs first
    cur.execute(
        "SELECT DISTINCT fy FROM user_uploaded_trades WHERE user_id = %s ORDER BY fy",
        (user_id,)
    )
    uploaded_fys = [r["fy"] for r in cur.fetchall()]

    for ufy in uploaded_fys:
        b = get_user_base_capital(cur, user_id, ufy)
        if b is not None:
            parts.append(
                f"SELECT {cols} FROM {UPLOADED_TRADES_TABLE} "
                f"WHERE user_id = '{user_id}' AND fy = '{ufy}'"
            )
            uploaded_fys_included.add(ufy)
            if earliest_base is None:
                earliest_base = b

    # 2. Include legacy tables (skip if user has uploaded data for that FY).
    #    Legacy tables have user_id — MUST filter. Previously unfiltered
    #    path caused a cross-user leak (user B saw user A's win-rate by FY).
    for fy, tbl in LEGACY_FY_TABLES.items():
        if fy in uploaded_fys_included:
            continue
        b = get_user_base_capital(cur, user_id, fy)
        if b is not None:
            parts.append(f"SELECT {cols} FROM {tbl} WHERE user_id = '{user_id}'")
            if earliest_base is None:
                earliest_base = b

    # 3. Journal FYs (skip legacy + uploaded FYs)
    skip_fys = set(LEGACY_FY_TABLES.keys()) | uploaded_fys_included
    cur.execute(
        "SELECT fy FROM user_fy_config WHERE user_id = %s AND fy NOT IN %s ORDER BY fy",
        (user_id, tuple(skip_fys) if skip_fys else ('__none__',))
    )
    journal_fys = [r["fy"] for r in cur.fetchall()]

    if journal_fys:
        parts.append(
            f"SELECT {cols} FROM {JOURNAL_FY_TABLE} WHERE user_id = '{user_id}'"
        )
        if earliest_base is None:
            earliest_base = get_user_base_capital(cur, user_id, journal_fys[0])

    if not parts:
        return {"allowed": False, "needs_setup": True, "role": role}

    union_sql = " UNION ALL ".join(parts)
    tbl = f"({union_sql}) combined"

    return {
        "table": tbl,
        "base": earliest_base or 6000000,
        "user_filter": False,  # Already filtered in the UNION subquery
        "allowed": True,
        "role": role,
    }
