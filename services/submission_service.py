"""
Submission Service - Database operations for submissions
"""
from database.database import get_db
from datetime import datetime
import json


def safe_float(value):
    """Convert value to float, return None if empty or invalid"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def safe_int(value):
    """Convert value to int, return None if empty or invalid"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def save_submission(data, scores, final_score, rating, user_id):
    """Save submission to database — deduplicates within 21 days (updates existing)"""
    conn = get_db()
    cursor = conn.cursor()

    try:
        stock_name = data.get('stock_name')
        print(f"\n📝 SAVING SUBMISSION: {stock_name}")
        print(f"   Setup: {data.get('setup_type', 'large_base_institutional')}")
        print(f"   Final Score: {final_score} | Rating: {rating}")

        # Check for existing submission of same stock within last 21 days
        cursor.execute('''
            SELECT id FROM submissions
            WHERE stock_name = %s AND user_id = %s AND timestamp >= NOW() - INTERVAL '21 days'
            ORDER BY timestamp DESC LIMIT 1
        ''', (stock_name, user_id))
        existing = cursor.fetchone()
        
        if existing:
            # Update existing record instead of creating duplicate
            existing_id = existing['id']
            print(f"   ♻️  Updating existing submission #{existing_id} (same stock within 21 days)")
            cursor.execute('''
                UPDATE submissions SET
                    market_price = %s, market_cap = %s, liquidity = %s, adr = %s,
                    freefloat = %s, sector = %s, linearity = %s, sector_strength = %s,
                    symmetry = %s, institutional_participation = %s, relative_strength = %s,
                    market_trend = %s, previous_move_enabled = %s,
                    move_percentage = %s, move_days = %s, move_ema = %s,
                    market_cap_score = %s, linearity_score = %s, sector_strength_score = %s,
                    symmetry_score = %s, institutional_participation_score = %s,
                    relative_strength_score = %s, liquidity_score = %s, adr_score = %s,
                    market_trend_score = %s, ferocity_score = %s, magnitude_score = %s,
                    move_ema_score = %s, final_score = %s, rating = %s,
                    chart_image_path = %s, quarterly_results = %s, timestamp = %s,
                    setup_type = %s, extension_pct = %s, extension_base_score = %s, move_quality_score = %s,
                    security_id = %s, symbol = %s
                WHERE id = %s AND user_id = %s
                RETURNING id
            ''', (
                safe_float(data.get('market_price')),
                safe_float(data.get('market_cap')),
                safe_float(data.get('liquidity')),
                safe_float(data.get('adr')),
                safe_float(data.get('freefloat')),
                data.get('sector') or None,
                data.get('linearity', 'Good'),
                data.get('sector_strength', False),
                data.get('symmetry', False),
                data.get('institutional_participation', False),
                data.get('relative_strength', False),
                data.get('market_trend', 'Uptrend'),
                data.get('previous_move_enabled', False),
                safe_float(data.get('move_percentage')),
                safe_int(data.get('move_days')),
                data.get('move_ema') or None,
                scores.get('market_cap', 0),
                scores.get('linearity', 0),
                scores.get('sector_strength', 0),
                scores.get('symmetry', 0),
                scores.get('institutional_participation', 0),
                scores.get('relative_strength', 0),
                scores.get('liquidity', 0),
                scores.get('adr', 0),
                scores.get('market_trend', 0),
                scores.get('ferocity', 0),
                scores.get('magnitude', 0),
                scores.get('move_ema', 0),
                final_score,
                rating,
                data.get('chart_image_path') or None,
                json.dumps(data.get('quarterly_results')) if data.get('quarterly_results') else None,
                datetime.now().isoformat(),
                data.get('setup_type', 'large_base_institutional'),
                safe_float(data.get('extension_pct')),
                scores.get('extension_base', 0),
                scores.get('move_quality', 0),
                data.get('security_id') or None,
                data.get('symbol') or None,
                existing_id,
                user_id,
            ))
            row = cursor.fetchone()
            submission_id = row['id'] if row else existing_id
            conn.commit()
            print(f"   ✅ Updated submission #{submission_id}")
            return submission_id
        
        # No recent duplicate — insert new
        cursor.execute('''
            INSERT INTO submissions (
                stock_name, market_price, market_cap, liquidity, adr,
                freefloat, sector, linearity, sector_strength, symmetry,
                institutional_participation, relative_strength,
                market_trend, previous_move_enabled, move_percentage, move_days, move_ema,
                market_cap_score, linearity_score, sector_strength_score,
                symmetry_score, institutional_participation_score,
                relative_strength_score, liquidity_score, adr_score,
                market_trend_score, ferocity_score, magnitude_score, move_ema_score,
                final_score, rating, chart_image_path, quarterly_results, timestamp,
                setup_type, extension_pct, extension_base_score, move_quality_score,
                security_id, symbol, user_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            data.get('stock_name'),
            safe_float(data.get('market_price')),
            safe_float(data.get('market_cap')),
            safe_float(data.get('liquidity')),
            safe_float(data.get('adr')),
            safe_float(data.get('freefloat')),
            data.get('sector') or None,
            data.get('linearity', 'Good'),
            data.get('sector_strength', False),
            data.get('symmetry', False),
            data.get('institutional_participation', False),
            data.get('relative_strength', False),
            data.get('market_trend', 'Uptrend'),
            data.get('previous_move_enabled', False),
            safe_float(data.get('move_percentage')),
            safe_int(data.get('move_days')),
            data.get('move_ema') or None,
            scores.get('market_cap', 0),
            scores.get('linearity', 0),
            scores.get('sector_strength', 0),
            scores.get('symmetry', 0),
            scores.get('institutional_participation', 0),
            scores.get('relative_strength', 0),
            scores.get('liquidity', 0),
            scores.get('adr', 0),
            scores.get('market_trend', 0),
            scores.get('ferocity', 0),
            scores.get('magnitude', 0),
            scores.get('move_ema', 0),
            final_score,
            rating,
            data.get('chart_image_path') or None,
            json.dumps(data.get('quarterly_results')) if data.get('quarterly_results') else None,
            datetime.now().isoformat(),
            data.get('setup_type', 'large_base_institutional'),
            safe_float(data.get('extension_pct')),
            scores.get('extension_base', 0),
            scores.get('move_quality', 0),
            data.get('security_id') or None,
            data.get('symbol') or None,
            user_id,
        ))

        row = cursor.fetchone()
        submission_id = row['id'] if row else None
        conn.commit()
        print(f"   ✅ New submission #{submission_id}")
        return submission_id
    except Exception as e:
        print(f"   ❌ Database error: {str(e)}")
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_all_submissions(page=1, per_page=10, user_id=None):
    """Get paginated submissions for a specific user"""
    conn = get_db()
    cursor = conn.cursor()

    try:
        if user_id:
            cursor.execute('SELECT COUNT(*) as count FROM submissions WHERE user_id = %s', (user_id,))
        else:
            cursor.execute('SELECT COUNT(*) as count FROM submissions')
        total = cursor.fetchone()['count']

        offset = (page - 1) * per_page
        if user_id:
            cursor.execute('''
                SELECT * FROM submissions WHERE user_id = %s
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            ''', (user_id, per_page, offset))
        else:
            cursor.execute('''
                SELECT * FROM submissions
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            ''', (per_page, offset))

        submissions = [dict(row) for row in cursor.fetchall()]

        return {
            'submissions': submissions,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        }
    finally:
        conn.close()


def get_submission_by_id(submission_id, user_id=None):
    """Get specific submission for a specific user"""
    conn = get_db()
    cursor = conn.cursor()

    try:
        if user_id:
            cursor.execute('SELECT * FROM submissions WHERE id = %s AND user_id = %s', (submission_id, user_id))
        else:
            cursor.execute('SELECT * FROM submissions WHERE id = %s', (submission_id,))
        submission = cursor.fetchone()
        
        if submission:
            return dict(submission)
        return None
    finally:
        conn.close()


def update_submission(submission_id, data, scores, final_score, rating, user_id):
    """Update an existing submission with new params and rescored values"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE submissions SET
                linearity = %s, sector_strength = %s, symmetry = %s,
                institutional_participation = %s, relative_strength = %s,
                market_trend = %s, previous_move_enabled = %s,
                move_percentage = %s, move_days = %s, move_ema = %s,
                extension_pct = %s,
                market_cap_score = %s, linearity_score = %s, sector_strength_score = %s,
                symmetry_score = %s, institutional_participation_score = %s,
                relative_strength_score = %s, liquidity_score = %s, adr_score = %s,
                market_trend_score = %s, ferocity_score = %s, magnitude_score = %s,
                move_ema_score = %s, extension_base_score = %s, move_quality_score = %s,
                final_score = %s, rating = %s
            WHERE id = %s AND user_id = %s
            RETURNING id
        ''', (
            data.get('linearity', 'Good'),
            data.get('sector_strength', False),
            data.get('symmetry', False),
            data.get('institutional_participation', False),
            data.get('relative_strength', False),
            data.get('market_trend', 'Uptrend'),
            data.get('previous_move_enabled', False),
            safe_float(data.get('move_percentage')),
            safe_int(data.get('move_days')),
            data.get('move_ema') or None,
            safe_float(data.get('extension_pct')),
            scores.get('market_cap', 0),
            scores.get('linearity', 0),
            scores.get('sector_strength', 0),
            scores.get('symmetry', 0),
            scores.get('institutional_participation', 0),
            scores.get('relative_strength', 0),
            scores.get('liquidity', 0),
            scores.get('adr', 0),
            scores.get('market_trend', 0),
            scores.get('ferocity', 0),
            scores.get('magnitude', 0),
            scores.get('move_ema', 0),
            scores.get('extension_base', 0),
            scores.get('move_quality', 0),
            final_score,
            rating,
            submission_id,
            user_id,
        ))
        updated = cursor.fetchone()
        conn.commit()
        return updated is not None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def delete_submission(submission_id, user_id):
    """Delete a submission by ID (scoped to user)"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        if user_id:
            cursor.execute('DELETE FROM submissions WHERE id = %s AND user_id = %s', (submission_id, user_id))
        else:
            cursor.execute('DELETE FROM submissions WHERE id = %s', (submission_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def check_duplicates(stock_name, days=7, user_id=None):
    """Check if stock_name or symbol exists in submissions from last N days"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        if user_id:
            cursor.execute('''
                SELECT id, stock_name, symbol, final_score, rating, setup_type, timestamp
                FROM submissions
                WHERE user_id = %s
                AND (LOWER(stock_name) = LOWER(%s) OR LOWER(symbol) = LOWER(%s))
                AND timestamp::timestamp >= (NOW() - INTERVAL '%s days')
                ORDER BY timestamp DESC
            ''', (user_id, stock_name, stock_name, days))
        else:
            cursor.execute('''
                SELECT id, stock_name, symbol, final_score, rating, setup_type, timestamp
                FROM submissions
                WHERE (LOWER(stock_name) = LOWER(%s) OR LOWER(symbol) = LOWER(%s))
                AND timestamp::timestamp >= (NOW() - INTERVAL '%s days')
                ORDER BY timestamp DESC
            ''', (stock_name, stock_name, days))
        dupes = [dict(row) for row in cursor.fetchall()]
        return dupes
    finally:
        conn.close()


def delete_submissions_bulk(ids, user_id):
    """Delete multiple submissions by list of IDs (scoped to user)"""
    if not ids:
        return 0
    conn = get_db()
    cursor = conn.cursor()
    try:
        placeholders = ','.join(['%s'] * len(ids))
        if user_id:
            cursor.execute(f'DELETE FROM submissions WHERE id IN ({placeholders}) AND user_id = %s', tuple(ids) + (user_id,))
        else:
            cursor.execute(f'DELETE FROM submissions WHERE id IN ({placeholders})', tuple(ids))
        deleted = cursor.rowcount
        conn.commit()
        return deleted
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def update_trade(submission_id, traded, entry_price=None, position_size=None, exit_price=None, user_id=None):
    """Update trade tracking fields for a submission"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE submissions
            SET traded = %s, entry_price = %s, position_size = %s, exit_price = %s
            WHERE id = %s AND user_id = %s
        ''', (traded, entry_price, position_size, exit_price, submission_id, user_id))
        updated = cursor.rowcount > 0
        conn.commit()
        return updated
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
        