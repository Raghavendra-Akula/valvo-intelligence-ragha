"""
Valvo AI v4 -- Stock search via pgvector embeddings in PostgreSQL.

Embeddings stored permanently in stock_universe.embedding column.
One-time computation per stock. Incremental — only embeds new stocks.

Auto-refresh: periodic check (every 6 hours) catches new stocks
added by the daily universe_sync.py pipeline job.

Search: embed query → ORDER BY embedding <=> query_vector → instant.
No in-memory cache. No warm-up. Survives container restarts.
"""
from __future__ import annotations

import json
import os
import threading
import time

from database.database import get_db, close_db


EMBEDDING_MODEL = "text-embedding-004"
EMBEDDING_DIM = 768

# Periodic check — catches new stocks added by the daily pipeline
_last_check_time = 0.0
_CHECK_INTERVAL_SECONDS = 6 * 3600  # 6 hours
_check_lock = threading.Lock()


def _get_client():
    api_key = os.getenv("api_key", "").strip()
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


# ═══════════════════════════════════════════════════════════════════════════
#  Schema setup — add embedding column if missing
# ═══════════════════════════════════════════════════════════════════════════

def ensure_schema():
    """Add pgvector extension + embedding column to stock_universe if not present."""
    conn = get_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'stock_universe' AND column_name = 'embedding'
                ) THEN
                    ALTER TABLE stock_universe ADD COLUMN embedding vector(%s);
                END IF;
            END $$
        """, (EMBEDDING_DIM,))
        conn.commit()
        return True
    except Exception as e:
        print(f"[stock_embeddings] schema setup failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  Populate embeddings — only for stocks that don't have one yet
# ═══════════════════════════════════════════════════════════════════════════

def populate_missing_embeddings():
    """Embed stocks that have no embedding yet. Runs incrementally."""
    client = _get_client()
    if not client:
        print("[stock_embeddings] No API key — cannot populate")
        return 0

    conn = get_db()
    if not conn:
        return 0

    try:
        cur = conn.cursor()

        # Find stocks without embeddings
        cur.execute(
            "SELECT security_id, symbol, company_name FROM stock_universe "
            "WHERE is_active = true AND embedding IS NULL "
            "ORDER BY symbol LIMIT 500"
        )
        missing = cur.fetchall()

        if not missing:
            return 0

        print(f"[stock_embeddings] Embedding {len(missing)} stocks...")

        # Embed in batches of 100
        total = 0
        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            texts = [f"{r['symbol']} - {r['company_name']}" for r in batch]

            try:
                result = client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=texts,
                )
                vectors = [e.values for e in result.embeddings]

                # Store in database
                for j, row in enumerate(batch):
                    vec_str = "[" + ",".join(str(v) for v in vectors[j]) + "]"
                    cur.execute(
                        "UPDATE stock_universe SET embedding = %s WHERE security_id = %s",
                        (vec_str, row["security_id"]),
                    )
                conn.commit()
                total += len(batch)
                print(f"[stock_embeddings] Batch {i}-{i+len(batch)}: done ({total} total)")
            except Exception as e:
                print(f"[stock_embeddings] Batch {i} failed: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass

        print(f"[stock_embeddings] Populated {total} embeddings")
        return total
    except Exception as e:
        print(f"[stock_embeddings] populate failed: {e}")
        return 0
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  Search — embed query, cosine similarity via pgvector
# ═══════════════════════════════════════════════════════════════════════════

def _maybe_refresh_embeddings():
    """If it's been >6 hours since last check, refresh in background."""
    global _last_check_time
    now = time.time()
    with _check_lock:
        if now - _last_check_time < _CHECK_INTERVAL_SECONDS:
            return
        _last_check_time = now

    threading.Thread(target=populate_missing_embeddings, daemon=True).start()


def search_by_embedding(query: str, top_k: int = 10) -> list[dict]:
    """
    Search stocks using pgvector cosine similarity.
    Embeds the query, then: ORDER BY embedding <=> query_vec LIMIT top_k.
    """
    # Periodically refresh for new stocks (doesn't block search)
    _maybe_refresh_embeddings()

    client = _get_client()
    if not client:
        return []

    # Embed the query
    try:
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=[query],
        )
        query_vec = result.embeddings[0].values
    except Exception as e:
        print(f"[stock_embeddings] query embed failed: {e}")
        return []

    # Search using pgvector cosine distance
    conn = get_db()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"

        cur.execute("""
            SELECT security_id, symbol, company_name,
                   1 - (embedding <=> %s::vector) as similarity
            FROM stock_universe
            WHERE embedding IS NOT NULL AND is_active = true
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (vec_str, vec_str, top_k))

        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[stock_embeddings] search failed: {e}")
        return []
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  Startup — ensure schema + populate in background
# ═══════════════════════════════════════════════════════════════════════════

def preload_async():
    """Set up schema and populate missing embeddings in a background thread."""
    def _run():
        try:
            if ensure_schema():
                populate_missing_embeddings()
        except Exception as e:
            print(f"[stock_embeddings] preload failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
