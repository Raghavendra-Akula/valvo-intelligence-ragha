"""V2 schema loader — applies 2026_04_30_classification_v2.sql to the DB."""
from __future__ import annotations

import os

from database.database import get_db, close_db


_MIGRATION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "database",
    "migrations",
    "2026_04_30_classification_v2.sql",
)


def init_schema() -> dict:
    """Run the V2 migration. Idempotent — safe to re-invoke any time.
    Returns {'applied': bool, 'file': path}."""
    with open(_MIGRATION_FILE, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
    finally:
        close_db(conn)
    return {"applied": True, "file": _MIGRATION_FILE}
