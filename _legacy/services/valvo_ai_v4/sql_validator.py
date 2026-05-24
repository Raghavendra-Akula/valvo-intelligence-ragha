"""
Valvo AI v4 -- SQL Validator with auto-retry (Phase 2)

Wraps sql_query execution with:
1. Pre-execution validation (read-only check, syntax hints)
2. Post-execution validation (did it return data? any obvious errors?)
3. Auto-retry with LLM-based error correction

Based on MAC-SQL Refiner pattern — catches ~70% of SQL failures automatically.
"""
from __future__ import annotations

import json
import re
from typing import Any

from services.valvo_ai_v2.utils import to_jsonable
from database.database import get_db, close_db

from .gateway import GeminiFlashGateway, FLASH_LITE_MODEL


_DML_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CREATE|GRANT|REVOKE|VACUUM|COPY)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def _validate_sql(query: str) -> str | None:
    """Pre-execution validation. Returns error string or None if OK."""
    stripped = query.strip().rstrip(";").strip()
    if not stripped:
        return "Empty query"
    if not _ALLOWED_START.match(stripped):
        return "Only SELECT and WITH statements are allowed."
    sanitized = re.sub(r"'[^']*'", "''", stripped)
    if _DML_PATTERN.search(sanitized):
        return "Query contains forbidden DML keywords. Only SELECT/WITH queries are permitted."
    return None


def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


def _execute_raw(query: str) -> dict:
    """Execute a SQL query with RLS context. Returns dict with rows or error."""
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        if uid:
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(uid)}),),
            )
            cur.execute("SET LOCAL ROLE authenticated")

        # Statement timeout to prevent runaway queries
        cur.execute("SET LOCAL statement_timeout = '15s'")

        cur.execute(query)
        rows = cur.fetchmany(100)
        result = [dict(r) for r in rows]
        text = json.dumps(to_jsonable(result), default=str, ensure_ascii=False)
        if len(text) > 8000:
            text = text[:8000] + "...(truncated)"
        return {
            "rows": json.loads(text) if len(text) <= 8000 else result[:50],
            "count": len(result),
            "row_count": len(result),
        }
    except Exception as exc:
        err_str = str(exc)
        return {
            "error": err_str,
            "error_type": _classify_error(err_str),
        }
    finally:
        close_db(conn)


def _classify_error(err: str) -> str:
    """Classify SQL error for targeted correction."""
    err_lower = err.lower()
    if "does not exist" in err_lower and "column" in err_lower:
        return "unknown_column"
    if "does not exist" in err_lower:
        return "unknown_table"
    if "syntax error" in err_lower:
        return "syntax_error"
    if "statement timeout" in err_lower:
        return "timeout"
    if "permission denied" in err_lower:
        return "permission"
    if "ambiguous" in err_lower:
        return "ambiguous_column"
    if "type" in err_lower and ("mismatch" in err_lower or "invalid" in err_lower):
        return "type_mismatch"
    return "unknown"


_CORRECTION_SYSTEM = """\
You are a PostgreSQL expert fixing a failed SQL query.
Given the original query, the error, and the schema, output ONLY the corrected SQL.
No markdown, no prose, no explanation — just the SQL statement ending with semicolon optional.
Keep the query's original intent. Only fix the specific error.
"""


def _attempt_correction(query: str, error: str, schema_hint: str, gateway: GeminiFlashGateway) -> str | None:
    """Use Flash Lite to fix the SQL query based on the error message."""
    if not gateway.available():
        return None

    user_prompt = f"""\
Failed query:
{query}

Error:
{error}

Relevant schema:
{schema_hint}

Output the corrected SQL only.
"""
    try:
        result = gateway.create_message(
            model_id=FLASH_LITE_MODEL,
            max_tokens=400,
            system=_CORRECTION_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[],
        )
        corrected = (result.text or "").strip()
        # Strip code fences if present
        if corrected.startswith("```"):
            corrected = corrected.split("```")[1] if "```" in corrected else corrected
            if corrected.startswith("sql"):
                corrected = corrected[3:].strip()
        # Validate it's still read-only
        if _validate_sql(corrected):
            return None
        return corrected
    except Exception as e:
        print(f"[sql_validator] correction failed: {e}")
        return None


def execute_validated_sql(params: dict, gateway: GeminiFlashGateway | None = None) -> dict:
    """
    Execute SQL with validation and auto-retry.

    Flow:
    1. Pre-validate (read-only check)
    2. Execute
    3. If error → classify error → attempt correction → retry (once)
    4. Return final result with retry metadata
    """
    query = (params.get("query") or "").strip()

    # Pre-validation
    pre_error = _validate_sql(query)
    if pre_error:
        return {"error": pre_error}

    # First attempt
    result = _execute_raw(query)
    if not result.get("error"):
        return result

    # Retry logic — only for correctable errors
    error_type = result.get("error_type", "unknown")
    if error_type in ("unknown_table", "unknown_column", "syntax_error", "ambiguous_column", "type_mismatch") and gateway:
        # Load schema hint for correction
        try:
            from .schema import ESCAPE_HATCH_SCHEMA
            schema_hint = ESCAPE_HATCH_SCHEMA[:2000]  # cap size
        except Exception:
            schema_hint = ""

        corrected = _attempt_correction(query, result["error"], schema_hint, gateway)
        if corrected and corrected != query:
            print(f"[sql_validator] retrying with corrected SQL: {corrected[:100]}")
            retry_result = _execute_raw(corrected)
            if not retry_result.get("error"):
                retry_result["_retried"] = True
                retry_result["_original_error"] = result["error"]
                return retry_result
            # If retry also failed, return both errors
            result["_retry_error"] = retry_result.get("error")
            result["_retry_sql"] = corrected

    # Add helpful hints to final error
    if error_type == "unknown_table":
        result["hint"] = "Check table names in the schema. Common mistake: legacy_trades_fy2425 (not legacy_trades_2425)."
    elif error_type == "unknown_column":
        result["hint"] = "Check column names. Common trade columns: symbol, realized_pl, realized_pl_pct, is_winner, month_label."
    elif error_type == "timeout":
        result["hint"] = "Query is too slow. Try a smaller date range or add LIMIT."
    elif error_type == "permission":
        result["hint"] = "Table requires user-specific filtering. Add 'WHERE user_id = <user_id>' or use a semantic tool."

    return result
