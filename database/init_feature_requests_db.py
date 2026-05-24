"""
Feature Requests tables — feature_requests, feature_votes, feature_embeddings,
feature_merge_log, and hybrid_feature_search RPC function.
Called on app startup to ensure tables + extensions exist.
"""
from database.database import get_db, close_db


def init_feature_requests_tables():
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()

        # ── Extensions ────────────────────────────────────────
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

        # ── Core table ────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feature_requests (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'open',
                vote_count INT NOT NULL DEFAULT 1,
                merged_into UUID REFERENCES feature_requests(id) ON DELETE SET NULL,
                is_merged BOOLEAN NOT NULL DEFAULT false,
                admin_response TEXT,
                admin_responded_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Votes (one per user per request) ──────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feature_votes (
                id SERIAL PRIMARY KEY,
                feature_id UUID NOT NULL REFERENCES feature_requests(id) ON DELETE CASCADE,
                user_id UUID NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(feature_id, user_id)
            )
        """)

        # ── Embeddings (768-dim Gemini embedding-001) ─────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feature_embeddings (
                feature_id UUID PRIMARY KEY REFERENCES feature_requests(id) ON DELETE CASCADE,
                embedding vector(768) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Merge audit log ───────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feature_merge_log (
                id SERIAL PRIMARY KEY,
                source_id UUID NOT NULL,
                target_id UUID NOT NULL,
                reason TEXT NOT NULL,
                votes_transferred INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # ── Indexes ───────────────────────────────────────────
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fr_status ON feature_requests(status) WHERE NOT is_merged")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fr_votes ON feature_requests(vote_count DESC) WHERE NOT is_merged")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fr_user ON feature_requests(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fr_title_trgm ON feature_requests USING gin (title gin_trgm_ops)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fv_feature ON feature_votes(feature_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fv_user ON feature_votes(user_id)")

        # HNSW index for fast cosine similarity on embeddings
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fe_cosine
            ON feature_embeddings USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)

        # ── Hybrid search function (pg_trgm + pgvector + RRF) ─
        cur.execute("""
            CREATE OR REPLACE FUNCTION hybrid_feature_search(
                query_text TEXT,
                query_embedding vector(768),
                match_count INT DEFAULT 5
            )
            RETURNS TABLE(
                id UUID,
                title TEXT,
                description TEXT,
                category TEXT,
                vote_count INT,
                status TEXT,
                rrf_score FLOAT
            ) AS $$
            WITH semantic AS (
                SELECT fr.id,
                       RANK() OVER (ORDER BY fe.embedding <=> query_embedding) AS rank
                FROM feature_embeddings fe
                JOIN feature_requests fr ON fr.id = fe.feature_id
                WHERE NOT fr.is_merged
                ORDER BY fe.embedding <=> query_embedding
                LIMIT 20
            ),
            keyword AS (
                SELECT fr.id,
                       RANK() OVER (ORDER BY similarity(fr.title, query_text) DESC) AS rank
                FROM feature_requests fr
                WHERE NOT fr.is_merged
                  AND similarity(fr.title, query_text) > 0.08
                ORDER BY similarity(fr.title, query_text) DESC
                LIMIT 20
            )
            SELECT fr.id, fr.title, fr.description, fr.category,
                   fr.vote_count, fr.status,
                   COALESCE(1.0/(60 + s.rank), 0.0)
                   + COALESCE(1.0/(60 + k.rank), 0.0) AS rrf_score
            FROM feature_requests fr
            LEFT JOIN semantic s ON s.id = fr.id
            LEFT JOIN keyword  k ON k.id = fr.id
            WHERE (s.id IS NOT NULL OR k.id IS NOT NULL)
            ORDER BY rrf_score DESC
            LIMIT match_count;
            $$ LANGUAGE sql STABLE;
        """)

        conn.commit()
        print("✅ Feature requests tables ready")
    except Exception as e:
        conn.rollback()
        print(f"⚠️ Feature requests tables init: {e}")
    finally:
        close_db(conn)
