"""
Shared scaffolding for the Valvo-AI regression harness.

We stub out Flask + psycopg2 at import time so the action tests can run
anywhere (local laptop, CI container) without a real DB or request
context. Each test composes its own MockCursor row set, which is what
_find_active_position ultimately drives off.

Goal: every bug we've hit this session becomes a one-line test case
below, and `python -m unittest discover Backend/tests` proves it can't
come back silently.
"""
from __future__ import annotations

import sys
import types


def _install_stubs():
    """Install minimal stubs for modules actions.py imports at top level.

    Only idempotent — safe to call from every test module. Each stub is
    narrow (just the names actions.py touches) so real behavior can't
    leak in and make a test pass for the wrong reason.
    """
    if "flask" not in sys.modules:
        flask_stub = types.ModuleType("flask")

        class _G:
            user_id = "test-user"

        flask_stub.g = _G()
        sys.modules["flask"] = flask_stub

    if "database" not in sys.modules:
        sys.modules["database"] = types.ModuleType("database")

    if "database.database" not in sys.modules:
        db_stub = types.ModuleType("database.database")
        db_stub.get_db = lambda: None
        db_stub.close_db = lambda conn: None
        sys.modules["database.database"] = db_stub

    if "database.init_valvo_ai_v2_db" not in sys.modules:
        mod = types.ModuleType("database.init_valvo_ai_v2_db")
        mod.init_valvo_ai_v2_tables = lambda: None
        sys.modules["database.init_valvo_ai_v2_db"] = mod

    # services is a PEP 420 namespace package (no __init__.py); let Python
    # discover it naturally from Backend/services. We only stub the one
    # submodule that actions.py imports at top level and that would drag in
    # real DB code if we let it resolve.
    if "services.journal_position_sync" not in sys.modules:
        jsp = types.ModuleType("services.journal_position_sync")
        jsp.TRAILING_ACTIVE_MODES = set()
        jsp.sync_journal_trade_to_position = lambda *a, **k: None
        jsp.sync_position_to_journal = lambda *a, **k: None
        sys.modules["services.journal_position_sync"] = jsp


class MockCursor:
    """Minimal DB cursor — feeds pre-canned rows back to the executor.

    The action code uses two patterns: cur.fetchall() for list reads and
    cur.fetchone() for row reads. We queue expected row-sets keyed on a
    counter; tests don't need to care about the actual SQL.
    """

    def __init__(self, rowsets=None):
        # rowsets: list of lists (for fetchall) OR list of dicts (for fetchone).
        # We flip a switch per .execute(...) — caller queues them in order.
        self._rowsets = list(rowsets or [])
        self._last_rows = []

    def execute(self, query, params=None):
        if self._rowsets:
            self._last_rows = self._rowsets.pop(0)
        else:
            self._last_rows = []

    def fetchall(self):
        rows = self._last_rows
        self._last_rows = []
        return rows

    def fetchone(self):
        rows = self._last_rows
        self._last_rows = []
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows
