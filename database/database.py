"""
Database connection pool — PostgreSQL (Supabase)

Connects via Supavisor TRANSACTION pooler (port 6543).
Transaction mode releases backend connections after each query,
allowing 200 pooler client slots to serve thousands of requests/sec.

Constraints (transaction mode):
  - No session-level SET (use SET LOCAL inside transactions)
  - No prepared statements (psycopg2 raw execute is fine)
  - conn.reset() replaced with rollback()
  - No LISTEN/NOTIFY
"""
import os
import time
import threading
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from dotenv import load_dotenv

load_dotenv()

_pool = None
_pool_lock = threading.Lock()

# Transaction pooler (port 6543) — connections released after each query
# Session pooler (port 5432) — holds connection for entire session (DON'T USE)
_TRANSACTION_POOL_PORT = 6543
_SESSION_POOL_PORT = 5432


def _resolve_db_port():
    raw_port = os.getenv('DB_PORT')
    try:
        port = int(raw_port) if raw_port else _TRANSACTION_POOL_PORT
    except (TypeError, ValueError):
        port = _TRANSACTION_POOL_PORT

    host = (os.getenv('DB_HOST') or "").strip().lower()
    if host.endswith("pooler.supabase.com") and port == _SESSION_POOL_PORT:
        print("⚠ DB_PORT=5432 points to Supavisor session mode; switching to 6543 transaction mode for stability")
        return _TRANSACTION_POOL_PORT
    return port


_DB_PORT = _resolve_db_port()


def get_db_port():
    return _DB_PORT


def _get_pool():
    """Get or create the connection pool (thread-safe singleton)."""
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            return _pool
        try:
            _pool = ThreadedConnectionPool(
                minconn=2,   # warm connections per worker — avoids reconnect latency
                maxconn=5,   # 3 workers × 5 × 6 instances = 90 (45% of 200 pooler slots)
                host=os.getenv('DB_HOST'),
                database=os.getenv('DB_NAME', 'postgres'),
                user=os.getenv('DB_USER', 'postgres'),
                password=os.getenv('DB_PASSWORD'),
                port=_DB_PORT,
                sslmode='require',
                cursor_factory=RealDictCursor,
                # NO options='-c statement_timeout=...' — session-level SET not supported
                # in transaction mode. Use SET LOCAL inside individual queries instead.
                connect_timeout=10,
            )
            print(f"✅ Connection pool created (min=2, max=5, port={_DB_PORT}, mode=transaction)")
        except Exception as e:
            print(f"❌ Pool creation error: {e}")
            raise
        return _pool


class PooledConnection:
    """Wraps psycopg2 connection so .close() returns it to pool."""

    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
        self._returned = False

    def close(self):
        if not self._returned:
            self._returned = True
            try:
                if self._conn and not self._conn.closed:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                self._pool.putconn(self._conn)
            except Exception:
                try:
                    self._pool.putconn(self._conn, close=True)
                except Exception:
                    pass

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    @property
    def closed(self):
        return self._returned or (self._conn and self._conn.closed)

    @property
    def autocommit(self):
        return self._conn.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self._conn.autocommit = value

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        self.close()
        return False


def get_db():
    """
    Get a connection from the pool.
    Waits up to 5 seconds if all connections are busy.
    """
    pool = _get_pool()
    deadline = time.monotonic() + 5
    while True:
        try:
            raw_conn = pool.getconn()
            return PooledConnection(raw_conn, pool)
        except (psycopg2.pool.PoolError, psycopg2.OperationalError) as e:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Database connection unavailable (waited 5s): {e}")
            time.sleep(0.3)


def close_db(conn):
    """Return connection to pool. Safe to call with None."""
    if conn:
        conn.close()
