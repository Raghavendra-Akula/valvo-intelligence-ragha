"""
Feature Requests Routes — submit, vote, search, list, cleanup.
Auth handled globally by app.before_request middleware.
Cron endpoint (run_cleanup) uses X-Cron-Secret instead of JWT.
"""
import os
import uuid
from flask import Blueprint, request, jsonify, g
from database.database import get_db, close_db
from extensions import limiter

feature_bp = Blueprint("feature", __name__)


# ═══════════════════════════════════════════════════════════
# LIST — paginated, sorted, filtered
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features", methods=["GET"])
@limiter.limit("60 per minute")
def list_features():
    """List all non-merged feature requests with vote status for current user."""
    sort = request.args.get("sort", "votes")  # votes | newest | oldest
    status = request.args.get("status")
    category = request.args.get("category")
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(50, int(request.args.get("per_page", 30)))
    offset = (page - 1) * per_page
    user_id = g.user_id

    order_map = {
        "votes": "fr.vote_count DESC, fr.created_at DESC",
        "newest": "fr.created_at DESC",
        "oldest": "fr.created_at ASC",
    }
    order_clause = order_map.get(sort, order_map["votes"])

    conn = get_db()
    try:
        cur = conn.cursor()

        # Build WHERE
        conditions = ["NOT fr.is_merged"]
        params = []
        if status:
            conditions.append("fr.status = %s")
            params.append(status)
        if category:
            conditions.append("fr.category = %s")
            params.append(category)
        where = " AND ".join(conditions)

        # Count
        cur.execute(f"SELECT COUNT(*) AS cnt FROM feature_requests fr WHERE {where}", params)
        total = cur.fetchone()["cnt"]

        # Fetch with has_voted
        cur.execute(f"""
            SELECT fr.id, fr.title, fr.description, fr.category, fr.status,
                   fr.vote_count, fr.admin_response, fr.admin_responded_at,
                   fr.created_at, fr.updated_at,
                   CASE WHEN fv.id IS NOT NULL THEN true ELSE false END AS has_voted
            FROM feature_requests fr
            LEFT JOIN feature_votes fv ON fv.feature_id = fr.id AND fv.user_id = %s
            WHERE {where}
            ORDER BY {order_clause}
            LIMIT %s OFFSET %s
        """, [user_id] + params + [per_page, offset])
        rows = [dict(r) for r in cur.fetchall()]

        return jsonify({"features": rows, "total": total, "page": page, "per_page": per_page})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# SUBMIT — create new feature request + embedding + auto-vote
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features", methods=["POST"])
@limiter.limit("10 per minute")
def create_feature():
    """Submit a new feature request. Generates embedding and auto-upvotes."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    category = (data.get("category") or "general").strip().lower()

    if not title or len(title) < 5:
        return jsonify({"error": "Title must be at least 5 characters"}), 400
    if len(title) > 200:
        return jsonify({"error": "Title must be under 200 characters"}), 400

    valid_categories = {
        "general", "scoring", "screener", "watchlist", "positions",
        "journal", "ai", "alerts", "ui", "mobile", "data", "integrations",
    }
    if category not in valid_categories:
        category = "general"

    user_id = g.user_id
    feature_id = str(uuid.uuid4())

    # Generate embedding
    embed_text = f"{title}. {description}" if description else title
    try:
        from services.embedding_service import generate_embedding
        embedding = generate_embedding(embed_text, task_type="RETRIEVAL_DOCUMENT")
    except Exception as e:
        print(f"Embedding generation failed: {e}")
        embedding = None

    conn = get_db()
    try:
        cur = conn.cursor()

        # Insert feature request
        cur.execute("""
            INSERT INTO feature_requests (id, user_id, title, description, category, vote_count)
            VALUES (%s, %s, %s, %s, %s, 1)
            RETURNING id, title, description, category, status, vote_count, created_at
        """, [feature_id, user_id, title, description, category])
        row = dict(cur.fetchone())

        # Auto-upvote by creator
        cur.execute("""
            INSERT INTO feature_votes (feature_id, user_id) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """, [feature_id, user_id])

        # Store embedding
        if embedding:
            cur.execute("""
                INSERT INTO feature_embeddings (feature_id, embedding) VALUES (%s, %s::vector)
            """, [feature_id, embedding])

        conn.commit()
        row["has_voted"] = True
        return jsonify(row), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# DETAIL — single feature request
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features/<feature_id>", methods=["GET"])
@limiter.limit("60 per minute")
def get_feature(feature_id):
    """Get a single feature request with vote status."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fr.*,
                   CASE WHEN fv.id IS NOT NULL THEN true ELSE false END AS has_voted
            FROM feature_requests fr
            LEFT JOIN feature_votes fv ON fv.feature_id = fr.id AND fv.user_id = %s
            WHERE fr.id = %s
        """, [g.user_id, feature_id])
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# VOTE — toggle upvote
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features/<feature_id>/vote", methods=["POST"])
@limiter.limit("30 per minute")
def toggle_vote(feature_id):
    """Toggle upvote on a feature request. Returns new vote_count and has_voted."""
    user_id = g.user_id
    conn = get_db()
    try:
        cur = conn.cursor()

        # Check feature exists and is not merged
        cur.execute("SELECT id FROM feature_requests WHERE id = %s AND NOT is_merged", [feature_id])
        if not cur.fetchone():
            return jsonify({"error": "Feature not found"}), 404

        # Try to insert vote; if exists, delete it (toggle)
        cur.execute("""
            DELETE FROM feature_votes WHERE feature_id = %s AND user_id = %s RETURNING id
        """, [feature_id, user_id])
        removed = cur.fetchone()

        if not removed:
            # Vote didn't exist — add it
            cur.execute("""
                INSERT INTO feature_votes (feature_id, user_id) VALUES (%s, %s)
            """, [feature_id, user_id])

        # Recount
        cur.execute("""
            UPDATE feature_requests
            SET vote_count = (SELECT COUNT(*) FROM feature_votes WHERE feature_id = %s),
                updated_at = NOW()
            WHERE id = %s
            RETURNING vote_count
        """, [feature_id, feature_id])
        new_count = cur.fetchone()["vote_count"]

        conn.commit()
        return jsonify({"vote_count": new_count, "has_voted": not removed})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# SEARCH — hybrid pg_trgm + pgvector + RRF
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features/search", methods=["GET"])
@limiter.limit("30 per minute")
def search_features():
    """Semantic + keyword hybrid search for similar feature requests."""
    q = (request.args.get("q") or "").strip()
    if not q or len(q) < 3:
        return jsonify({"results": []})

    # Generate query embedding
    try:
        from services.embedding_service import generate_embedding
        query_embedding = generate_embedding(q, task_type="RETRIEVAL_QUERY")
    except Exception as e:
        print(f"Search embedding failed: {e}")
        # Fallback to keyword-only search
        return _keyword_only_search(q)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM hybrid_feature_search(%s, %s::vector, 5)
        """, [q, query_embedding])
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"results": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


def _keyword_only_search(q):
    """Fallback search using only pg_trgm when embeddings fail."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, description, category, vote_count, status,
                   similarity(title, %s) AS sim
            FROM feature_requests
            WHERE NOT is_merged AND similarity(title, %s) > 0.08
            ORDER BY sim DESC
            LIMIT 5
        """, [q, q])
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"results": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# TITLES — lightweight preload for client-side uFuzzy
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features/titles", methods=["GET"])
@limiter.limit("30 per minute")
def list_titles():
    """Return all non-merged feature titles + IDs for client-side fuzzy search."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, category, vote_count
            FROM feature_requests
            WHERE NOT is_merged
            ORDER BY vote_count DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"titles": rows}), 200, {"Cache-Control": "public, max-age=60"}
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# ADMIN RESPOND — set status + response (admin only)
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features/<feature_id>/respond", methods=["PUT"])
@limiter.limit("30 per minute")
def admin_respond(feature_id):
    """Admin: update status and/or add a response."""
    # Check admin role
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT plan FROM user_subscriptions WHERE user_id = %s
        """, [g.user_id])
        row = cur.fetchone()
        # Simple admin check via app_config or subscription
        cur.execute("SELECT value FROM app_config WHERE key = 'admin_users'")
        admin_row = cur.fetchone()
        is_admin = False
        if admin_row:
            admin_ids = admin_row["value"] if isinstance(admin_row["value"], list) else []
            is_admin = g.user_id in admin_ids

        # Fallback: check auth metadata
        if not is_admin:
            cur.execute("""
                SELECT raw_user_meta_data->>'role' AS role
                FROM auth.users WHERE id = %s
            """, [g.user_id])
            u = cur.fetchone()
            is_admin = u and u.get("role") == "admin"

        if not is_admin:
            return jsonify({"error": "Admin access required"}), 403

        data = request.get_json(silent=True) or {}
        status = data.get("status")
        admin_response = data.get("admin_response")

        valid_statuses = {"open", "under_review", "planned", "in_progress", "completed", "declined"}

        updates = []
        params = []
        if status and status in valid_statuses:
            updates.append("status = %s")
            params.append(status)
        if admin_response is not None:
            updates.append("admin_response = %s")
            updates.append("admin_responded_at = NOW()")
            params.append(admin_response)
        if not updates:
            return jsonify({"error": "Nothing to update"}), 400

        updates.append("updated_at = NOW()")
        params.append(feature_id)

        cur.execute(f"""
            UPDATE feature_requests SET {', '.join(updates)}
            WHERE id = %s
            RETURNING id, status, admin_response, admin_responded_at
        """, params)
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404

        conn.commit()
        return jsonify(dict(row))
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# CLEANUP CRON — cluster + merge duplicates
# ═══════════════════════════════════════════════════════════

@feature_bp.route("/api/features/cleanup", methods=["POST"])
@limiter.limit("5 per minute")
def run_cleanup():
    """
    Daily cron job: find semantically similar feature requests,
    ask Gemini Flash to confirm duplicates, merge them.
    Uses X-Cron-Secret header for auth (exempt from JWT).
    """
    cron_secret = os.getenv("CRON_SECRET", "")
    if cron_secret:
        req_secret = request.headers.get("X-Cron-Secret", "")
        if req_secret != cron_secret:
            return jsonify({"error": "Unauthorized"}), 403

    conn = get_db()
    results = {"clusters_found": 0, "merges_executed": 0, "votes_transferred": 0}
    try:
        cur = conn.cursor()

        # Step 1: Find all non-merged embeddings
        cur.execute("""
            SELECT fe.feature_id, fr.title, fr.description, fr.vote_count
            FROM feature_embeddings fe
            JOIN feature_requests fr ON fr.id = fe.feature_id
            WHERE NOT fr.is_merged
            ORDER BY fr.created_at
        """)
        items = [dict(r) for r in cur.fetchall()]

        if len(items) < 2:
            return jsonify(results)

        # Step 2: Find clusters with high cosine similarity (> 0.88)
        clusters = _find_similar_clusters(cur, items, threshold=0.88)
        results["clusters_found"] = len(clusters)

        if not clusters:
            return jsonify(results)

        # Step 3: LLM review each cluster
        for cluster in clusters:
            merge_result = _llm_review_cluster(cluster)
            if not merge_result:
                continue

            primary_id = merge_result["primary_id"]
            duplicate_ids = merge_result["duplicate_ids"]
            reason = merge_result.get("reason", "AI-detected duplicate")

            for dup_id in duplicate_ids:
                # Transfer votes
                cur.execute("""
                    UPDATE feature_votes SET feature_id = %s
                    WHERE feature_id = %s
                    AND user_id NOT IN (SELECT user_id FROM feature_votes WHERE feature_id = %s)
                """, [primary_id, dup_id, primary_id])
                transferred = cur.rowcount

                # Delete remaining conflicting votes
                cur.execute("DELETE FROM feature_votes WHERE feature_id = %s", [dup_id])

                # Mark as merged
                cur.execute("""
                    UPDATE feature_requests
                    SET is_merged = true, merged_into = %s, updated_at = NOW()
                    WHERE id = %s
                """, [primary_id, dup_id])

                # Log
                cur.execute("""
                    INSERT INTO feature_merge_log (source_id, target_id, reason, votes_transferred)
                    VALUES (%s, %s, %s, %s)
                """, [dup_id, primary_id, reason, transferred])

                results["merges_executed"] += 1
                results["votes_transferred"] += transferred

            # Recount votes on primary
            cur.execute("""
                UPDATE feature_requests
                SET vote_count = (SELECT COUNT(*) FROM feature_votes WHERE feature_id = %s),
                    updated_at = NOW()
                WHERE id = %s
            """, [primary_id, primary_id])

        conn.commit()
        return jsonify(results)
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


def _find_similar_clusters(cur, items, threshold=0.88):
    """Find clusters of similar feature requests using pgvector cosine similarity."""
    if len(items) < 2:
        return []

    ids = [item["feature_id"] for item in items]
    id_to_item = {item["feature_id"]: item for item in items}

    # For each item, find others with similarity > threshold
    pairs = set()
    for fid in ids:
        cur.execute("""
            SELECT fe2.feature_id,
                   1 - (fe1.embedding <=> fe2.embedding) AS sim
            FROM feature_embeddings fe1, feature_embeddings fe2
            WHERE fe1.feature_id = %s
              AND fe2.feature_id != %s
              AND fe2.feature_id = ANY(%s)
              AND 1 - (fe1.embedding <=> fe2.embedding) > %s
        """, [fid, fid, ids, threshold])
        for row in cur.fetchall():
            a, b = sorted([fid, row["feature_id"]])
            pairs.add((a, b))

    if not pairs:
        return []

    # Union-find to group into clusters
    parent = {fid: fid for fid in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        union(a, b)

    groups = {}
    for fid in ids:
        root = find(fid)
        if root not in groups:
            groups[root] = []
        groups[root].append(id_to_item[fid])

    return [g for g in groups.values() if len(g) >= 2]


def _llm_review_cluster(cluster):
    """Ask Gemini Flash to determine which items are true duplicates."""
    try:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(
            api_key=os.getenv("api_key", ""),
            http_options={"timeout": 30},
        )

        items_text = "\n".join(
            f"- ID: {item['feature_id']} | Title: {item['title']} | "
            f"Description: {item.get('description', '')} | Votes: {item['vote_count']}"
            for item in cluster
        )

        prompt = f"""You are reviewing feature requests for duplicates.
Given these feature requests that are semantically similar, determine which are truly
the same feature request (just worded differently) vs which are distinct requests.

For true duplicates, pick the one with the most votes (or best wording if tied) as the primary.

Requests:
{items_text}

Return ONLY valid JSON (no markdown):
{{"primary_id": "<id of the primary request to keep>", "duplicate_ids": ["<id1>", "<id2>"], "reason": "<one-line explanation>"}}

If they are actually different requests, return: {{"primary_id": null, "duplicate_ids": [], "reason": "distinct requests"}}"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        import json
        result = json.loads(response.text)
        if result.get("primary_id") and result.get("duplicate_ids"):
            return result
        return None
    except Exception as e:
        print(f"LLM cluster review failed: {e}")
        return None
