"""
Project Hub Routes — Projects + Task CRUD, team members, email notifications, reminder runner.
Auth handled globally by app.before_request middleware.
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from database.database import get_db, close_db
from datetime import date, timedelta
import json as _json

project_hub_bp = Blueprint("project_hub", __name__)


# ═══════════════════════════════════════════════════════════
# TEAM MEMBERS — for assignee dropdown
# ═══════════════════════════════════════════════════════════

@project_hub_bp.route("/api/project-hub/team", methods=["GET"])
@limiter.limit("60 per minute")
def get_team():
    """Returns list of admin users for assignee dropdown."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, email, display_name FROM team_members ORDER BY display_name")
        members = [dict(r) for r in cur.fetchall()]
        return jsonify({"members": members})
    except Exception as e:
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# PROJECTS CRUD
# ═══════════════════════════════════════════════════════════

@project_hub_bp.route("/api/project-hub/projects", methods=["GET"])
@limiter.limit("60 per minute")
def get_projects():
    """Returns all projects with task counts."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.*,
                COUNT(t.id) FILTER (WHERE t.id IS NOT NULL) AS task_count,
                COUNT(t.id) FILTER (WHERE t.status = 'done') AS done_count,
                COUNT(t.id) FILTER (WHERE t.status != 'done') AS active_count
            FROM hub_projects p
            LEFT JOIN project_tasks t ON t.project_id = p.id
            WHERE p.is_archived = FALSE
            GROUP BY p.id
            ORDER BY CASE WHEN p.id = 'general' THEN 1 ELSE 0 END, p.name
        """)
        projects = [dict(r) for r in cur.fetchall()]
        for p in projects:
            if p.get("created_at"): p["created_at"] = str(p["created_at"])
            if p.get("updated_at"): p["updated_at"] = str(p["updated_at"])
        return jsonify({"projects": projects})
    except Exception as e:
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@project_hub_bp.route("/api/project-hub/projects", methods=["POST"])
@limiter.limit("30 per minute")
def create_project():
    """Create a new project."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    import uuid
    project_id = str(uuid.uuid4())[:8]
    color = data.get("color", "#0A84FF")
    icon = data.get("icon", "📁")
    description = data.get("description", "")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO hub_projects (id, name, description, color, icon, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (project_id, name, description, color, icon, g.user_id))
        project = dict(cur.fetchone())
        conn.commit()
        for k in ["created_at", "updated_at"]:
            if project.get(k): project[k] = str(project[k])
        project["task_count"] = 0
        project["done_count"] = 0
        project["active_count"] = 0
        return jsonify({"project": project}), 201
    except Exception as e:
        conn.rollback()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@project_hub_bp.route("/api/project-hub/projects/<project_id>", methods=["PUT"])
@limiter.limit("30 per minute")
def update_project(project_id):
    """Update a project."""
    data = request.get_json() or {}
    conn = get_db()
    try:
        cur = conn.cursor()
        fields = []
        values = []
        for key in ["name", "description", "color", "icon", "is_archived"]:
            if key in data:
                fields.append(f"{key} = %s")
                values.append(data[key])
        if not fields:
            return jsonify({"error": "No fields to update"}), 400
        fields.append("updated_at = NOW()")
        values.append(project_id)
        cur.execute(
            f"UPDATE hub_projects SET {', '.join(fields)} WHERE id = %s RETURNING *",
            values
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Project not found"}), 404
        project = dict(row)
        conn.commit()
        for k in ["created_at", "updated_at"]:
            if project.get(k): project[k] = str(project[k])
        return jsonify({"project": project})
    except Exception as e:
        conn.rollback()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@project_hub_bp.route("/api/project-hub/projects/<project_id>", methods=["DELETE"])
@limiter.limit("15 per minute")
def delete_project(project_id):
    """Delete a project. Moves tasks to General."""
    if project_id == "general":
        return jsonify({"error": "Cannot delete the General project"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        # Move tasks to general
        cur.execute("UPDATE project_tasks SET project_id = 'general' WHERE project_id = %s", (project_id,))
        cur.execute("DELETE FROM hub_projects WHERE id = %s RETURNING id", (project_id,))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Project not found"}), 404
        return jsonify({"deleted": project_id})
    except Exception as e:
        conn.rollback()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# TASK CRUD
# ═══════════════════════════════════════════════════════════

def _serialize_task_dates(t):
    for k in ["due_date", "created_at", "updated_at", "completed_at"]:
        if t.get(k): t[k] = str(t[k])

@project_hub_bp.route("/api/project-hub/tasks", methods=["GET"])
@limiter.limit("60 per minute")
def get_tasks():
    """Returns tasks, optionally filtered by status and/or project_id."""
    status = request.args.get("status")
    project_id = request.args.get("project_id")
    conn = get_db()
    try:
        cur = conn.cursor()
        where = []
        params = []
        if status:
            where.append("t.status = %s")
            params.append(status)
        if project_id:
            where.append("t.project_id = %s")
            params.append(project_id)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(f"""
            SELECT t.*, tm.display_name as assignee_display
            FROM project_tasks t
            LEFT JOIN team_members tm ON tm.user_id = t.assignee_id
            {where_sql}
            ORDER BY t.column_order, t.created_at
        """, params)
        tasks = [dict(r) for r in cur.fetchall()]
        for t in tasks:
            _serialize_task_dates(t)
        return jsonify({"tasks": tasks})
    except Exception as e:
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@project_hub_bp.route("/api/project-hub/tasks", methods=["POST"])
@limiter.limit("30 per minute")
def create_task():
    """Create a new task. Sends assignment email if assignee is set."""
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400

    import uuid
    task_id = data.get("id") or str(uuid.uuid4())[:8]
    assignee_id = data.get("assignee_id")
    due_date = data.get("due_date") or None
    priority = data.get("priority", "medium")
    description = data.get("description", "")
    status = data.get("status", "todo")
    labels = data.get("labels", [])
    subtasks = data.get("subtasks", [])
    column_order = data.get("column_order", 0)
    project_id = data.get("project_id", "general")

    conn = get_db()
    try:
        cur = conn.cursor()

        # Resolve assignee name
        assignee_name = None
        if assignee_id:
            cur.execute("SELECT display_name FROM team_members WHERE user_id = %s", (assignee_id,))
            row = cur.fetchone()
            if row:
                assignee_name = row["display_name"]

        cur.execute("""
            INSERT INTO project_tasks (id, title, description, status, priority,
                assignee_id, assignee_name, due_date, labels, subtasks, column_order, created_by, project_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING *
        """, (task_id, title, description, status, priority,
              assignee_id, assignee_name, due_date, labels,
              _json.dumps(subtasks), column_order, g.user_id, project_id))
        task = dict(cur.fetchone())
        conn.commit()

        # Send assignment email
        if assignee_id:
            _send_assignment_email(cur, task, g.user_id)

        _serialize_task_dates(task)
        return jsonify({"task": task}), 201
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@project_hub_bp.route("/api/project-hub/tasks/<task_id>", methods=["PUT"])
@limiter.limit("30 per minute")
def update_task(task_id):
    """Update a task. Sends assignment email if assignee changes."""
    data = request.get_json() or {}
    conn = get_db()
    try:
        cur = conn.cursor()

        # Get current task
        cur.execute("SELECT * FROM project_tasks WHERE id = %s", (task_id,))
        old_task = cur.fetchone()
        if not old_task:
            return jsonify({"error": "Task not found"}), 404
        old_task = dict(old_task)
        old_assignee = old_task.get("assignee_id")

        # Build dynamic update
        fields = []
        values = []
        for key in ["title", "description", "status", "priority", "assignee_id",
                     "due_date", "column_order", "project_id"]:
            if key in data:
                fields.append(f"{key} = %s")
                values.append(data[key] if data[key] != "" else None)

        if "labels" in data:
            fields.append("labels = %s")
            values.append(data["labels"])
        if "subtasks" in data:
            fields.append("subtasks = %s::jsonb")
            values.append(_json.dumps(data["subtasks"]))

        # Resolve assignee name if assignee changed
        new_assignee = data.get("assignee_id", old_assignee)
        if "assignee_id" in data:
            if new_assignee:
                cur.execute("SELECT display_name FROM team_members WHERE user_id = %s", (new_assignee,))
                row = cur.fetchone()
                fields.append("assignee_name = %s")
                values.append(row["display_name"] if row else None)
            else:
                fields.append("assignee_name = %s")
                values.append(None)

        # Mark completed_at
        if data.get("status") == "done" and old_task.get("status") != "done":
            fields.append("completed_at = NOW()")
        elif data.get("status") and data.get("status") != "done":
            fields.append("completed_at = NULL")

        fields.append("updated_at = NOW()")
        values.append(task_id)

        cur.execute(
            f"UPDATE project_tasks SET {', '.join(fields)} WHERE id = %s RETURNING *",
            values
        )
        task = dict(cur.fetchone())
        conn.commit()

        # Send assignment email if assignee changed
        if "assignee_id" in data and str(data["assignee_id"] or "") != str(old_assignee or ""):
            if data["assignee_id"]:
                _send_assignment_email(cur, task, g.user_id)

        _serialize_task_dates(task)
        return jsonify({"task": task})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@project_hub_bp.route("/api/project-hub/tasks/<task_id>", methods=["DELETE"])
@limiter.limit("15 per minute")
def delete_task(task_id):
    """Delete a task."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM project_tasks WHERE id = %s RETURNING id", (task_id,))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Task not found"}), 404
        return jsonify({"deleted": task_id})
    except Exception as e:
        conn.rollback()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@project_hub_bp.route("/api/project-hub/tasks/reorder", methods=["POST"])
@limiter.limit("30 per minute")
def reorder_tasks():
    """Batch update status and column_order for drag-and-drop."""
    data = request.get_json() or {}
    updates = data.get("updates", [])
    if not updates:
        return jsonify({"error": "updates required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        for u in updates:
            cur.execute(
                "UPDATE project_tasks SET status = %s, column_order = %s, updated_at = NOW() WHERE id = %s",
                (u["status"], u.get("column_order", 0), u["id"])
            )
        conn.commit()
        return jsonify({"updated": len(updates)})
    except Exception as e:
        conn.rollback()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# TEST EMAIL — verify SMTP works
# ═══════════════════════════════════════════════════════════

@project_hub_bp.route("/api/project-hub/test-email", methods=["POST"])
@limiter.limit("30 per minute")
def test_email():
    """Send a test email to the current user to verify SMTP is working."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT email, display_name FROM user_profiles WHERE user_id = %s", (g.user_id,))
        row = cur.fetchone()
        if not row or not row["email"]:
            return jsonify({"error": "No email found for your account"}), 400

        from services.task_email_service import _send_email, _base_template
        content = """
        <div style="font-size:13px;color:#48bb78;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">✅ SMTP Test Successful</div>
        <div style="font-size:18px;font-weight:800;color:#e2e8f0;margin-bottom:16px;">Email delivery is working!</div>
        <div style="color:#c9d1d9;font-size:14px;line-height:1.6;">
          This confirms that Valvo Intelligence can send email notifications to <strong>{email}</strong>.
          Task assignment alerts and due-date reminders will be delivered to this address.
        </div>
        """.replace("{email}", row["email"])

        result = _send_email(row["email"], "✅ Valvo — Email Test Successful", _base_template(content))
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[project_hub] error: {e}")
        print(f"[project_hub] error: {e}")
        return jsonify({"success": False, "message": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# MANUAL NOTIFY — send notification for a specific task
# ═══════════════════════════════════════════════════════════

@project_hub_bp.route("/api/project-hub/notify/<task_id>", methods=["POST"])
@limiter.limit("30 per minute")
def notify_task(task_id):
    """Manually send assignment + due-date notification for a specific task."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.*, tm.email AS assignee_email, tm.display_name AS assignee_display
            FROM project_tasks t
            LEFT JOIN team_members tm ON tm.user_id = t.assignee_id
            WHERE t.id = %s
        """, (task_id,))
        task = cur.fetchone()
        if not task:
            return jsonify({"error": "Task not found"}), 404
        task = dict(task)
        if not task.get("assignee_id") or not task.get("assignee_email"):
            return jsonify({"error": "Task has no assignee or assignee has no email"}), 400

        from services.task_email_service import send_task_assigned

        # Get sender name
        cur.execute("SELECT display_name FROM user_profiles WHERE user_id = %s", (g.user_id,))
        sender = cur.fetchone()
        sender_name = sender["display_name"] if sender else "Team"

        result = send_task_assigned(
            to_email=task["assignee_email"],
            to_name=task["assignee_display"],
            task_title=task["title"],
            task_description=task.get("description", ""),
            due_date=task.get("due_date"),
            priority=task.get("priority", "P3"),
            assigned_by=sender_name,
        )

        if result.get("success"):
            # Log notification
            cur.execute("""
                INSERT INTO task_notifications (task_id, type, sent_to, sent_date)
                VALUES (%s, 'assigned', %s, CURRENT_DATE)
                ON CONFLICT DO NOTHING
            """, (task_id, task["assignee_email"]))
            conn.commit()

        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[project_hub] error: {e}")
        print(f"[project_hub] error: {e}")
        return jsonify({"success": False, "message": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# REMINDER RUNNER — called by cron job
# ═══════════════════════════════════════════════════════════

@project_hub_bp.route("/api/project-hub/run-reminders", methods=["POST"])
@limiter.limit("30 per minute")
def run_reminders():
    """
    Process all due-date reminders. Designed to be called by a cron job.
    Checks: 1-day before, due today, overdue.
    Uses task_notifications table to prevent duplicate sends.
    Accepts optional X-Cron-Secret header for security.
    """
    # Optional cron secret check
    cron_secret = __import__("os").getenv("CRON_SECRET", "")
    if cron_secret:
        req_secret = request.headers.get("X-Cron-Secret", "")
        if req_secret != cron_secret:
            return jsonify({"error": "Unauthorized"}), 403

    from services.task_email_service import (
        send_reminder_1day, send_reminder_today, send_overdue
    )

    conn = get_db()
    results = {"sent": 0, "skipped": 0, "errors": 0, "details": []}
    try:
        cur = conn.cursor()
        today = date.today()
        tomorrow = today + timedelta(days=1)

        # Get all incomplete tasks with due dates and assignees
        cur.execute("""
            SELECT t.id, t.title, t.due_date, t.priority, t.assignee_id,
                   tm.display_name, tm.email
            FROM project_tasks t
            JOIN team_members tm ON tm.user_id = t.assignee_id
            WHERE t.status != 'done' AND t.due_date IS NOT NULL AND t.assignee_id IS NOT NULL
        """)
        tasks = [dict(r) for r in cur.fetchall()]

        for t in tasks:
            due = t["due_date"]
            email = t["email"]
            name = t["display_name"]
            task_id = t["id"]
            title = t["title"]
            priority = t["priority"] or "medium"

            # Determine which notification type applies
            notif_type = None
            days_overdue = 0
            if due == tomorrow:
                notif_type = "reminder_1day"
            elif due == today:
                notif_type = "reminder_today"
            elif due < today:
                notif_type = "overdue"
                days_overdue = (today - due).days

            if not notif_type:
                continue

            # Check if already sent today
            cur.execute("""
                SELECT 1 FROM task_notifications
                WHERE task_id = %s AND type = %s AND sent_to = %s AND sent_date = %s
            """, (task_id, notif_type, email, today))
            if cur.fetchone():
                results["skipped"] += 1
                continue

            # Send the email
            try:
                if notif_type == "reminder_1day":
                    r = send_reminder_1day(email, name, title, due, priority)
                elif notif_type == "reminder_today":
                    r = send_reminder_today(email, name, title, due, priority)
                elif notif_type == "overdue":
                    r = send_overdue(email, name, title, due, priority, days_overdue)

                if r.get("success"):
                    # Log to prevent re-sending
                    cur.execute("""
                        INSERT INTO task_notifications (task_id, type, sent_to, sent_date)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (task_id, notif_type, email, today))
                    conn.commit()
                    results["sent"] += 1
                    results["details"].append(f"✅ {notif_type} → {name}: {title}")
                else:
                    results["errors"] += 1
                    results["details"].append(f"❌ {notif_type} → {name}: {r.get('message')}")
            except Exception as e:
                results["errors"] += 1
                results["details"].append(f"❌ {notif_type} → {name}: {str(e)}")

        return jsonify(results)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════
# HELPER — send assignment notification
# ═══════════════════════════════════════════════════════════

def _send_assignment_email(cur, task, assigned_by_id):
    """Send assignment notification email. Non-blocking — errors are logged but don't fail the request."""
    try:
        from services.task_email_service import send_task_assigned

        # Get assignee info
        cur.execute("SELECT email, display_name FROM team_members WHERE user_id = %s", (task["assignee_id"],))
        assignee = cur.fetchone()
        if not assignee:
            return

        # Get assigner name
        cur.execute("SELECT display_name FROM user_profiles WHERE user_id = %s", (assigned_by_id,))
        assigner = cur.fetchone()
        assigner_name = assigner["display_name"] if assigner else "Someone"

        result = send_task_assigned(
            to_email=assignee["email"],
            to_name=assignee["display_name"],
            task_title=task["title"],
            task_description=task.get("description", ""),
            due_date=task.get("due_date"),
            priority=task.get("priority", "medium"),
            assigned_by=assigner_name,
        )

        if result.get("success"):
            # Log the notification
            from database.database import get_db as _gdb
            conn2 = _gdb()
            cur2 = conn2.cursor()
            cur2.execute("""
                INSERT INTO task_notifications (task_id, type, sent_to, sent_date)
                VALUES (%s, 'assigned', %s, CURRENT_DATE)
                ON CONFLICT DO NOTHING
            """, (task["id"], assignee["email"]))
            conn2.commit()
            from database.database import close_db as _cdb
            _cdb(conn2)
            print(f"✅ Assignment email sent to {assignee['email']} for task '{task['title']}'")
        else:
            print(f"⚠️ Assignment email failed: {result.get('message')}")
    except Exception as e:
        print(f"⚠️ Assignment email error (non-fatal): {e}")


# ═══════════════════════════════════════════════════════════
# ONE-TIME MIGRATION — pull old tasks from Storage into DB
# ═══════════════════════════════════════════════════════════

@project_hub_bp.route("/api/project-hub/migrate-from-storage", methods=["POST"])
@limiter.limit("30 per minute")
def migrate_from_storage():
    """One-time: read tasks/board.json from Supabase Storage, insert into project_tasks table."""
    import os, urllib.request

    sb_url = os.getenv("SUPABASE_URL", "https://sxyktzpiixmidlxxfgdd.supabase.co")
    sb_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not sb_key:
        return jsonify({"error": "Supabase key is not configured"}), 500

    # Download board.json from Supabase Storage via REST API
    storage_url = f"{sb_url}/storage/v1/object/project-hub/tasks/board.json"
    req = urllib.request.Request(storage_url, headers={
        "Authorization": f"Bearer {sb_key}",
        "apikey": sb_key,
    })
    try:
        with urllib.request.urlopen(req) as resp:
            board = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[project_hub] error: {e}")
        print(f"[project_hub] storage error: {e}")
        return jsonify({"error": "Could not read Storage"}), 500

    tasks = board.get("tasks", {})
    columns = board.get("columns", {})
    if not tasks:
        return jsonify({"message": "No tasks found in Storage", "count": 0})

    # Build task → column mapping
    task_to_col = {}
    for col_id, col in columns.items():
        for i, tid in enumerate(col.get("taskIds", [])):
            task_to_col[tid] = {"status": col_id, "order": i}

    conn = get_db()
    migrated = 0
    skipped = 0
    errors = []
    try:
        cur = conn.cursor()
        for tid, t in tasks.items():
            # Check if already exists
            cur.execute("SELECT 1 FROM project_tasks WHERE id = %s", (tid,))
            if cur.fetchone():
                skipped += 1
                continue

            col_info = task_to_col.get(tid, {"status": "todo", "order": 0})
            status = col_info["status"]
            # Validate status
            if status not in ("backlog", "todo", "in-progress", "review", "done"):
                status = "todo"

            try:
                cur.execute("""
                    INSERT INTO project_tasks (id, title, description, status, priority,
                        assignee_name, due_date, labels, subtasks, column_order, created_by, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    tid,
                    t.get("title", "Untitled"),
                    t.get("description", ""),
                    status,
                    t.get("priority", "P3"),
                    t.get("assignee", "") or None,
                    t.get("dueDate", "") or None,
                    t.get("labels", []),
                    _json.dumps(t.get("subtasks", [])),
                    col_info["order"],
                    g.user_id,
                    t.get("createdAt", None),
                ))
                migrated += 1
            except Exception as e:
                errors.append(f"{tid}: {str(e)}")
                conn.rollback()

        conn.commit()
        return jsonify({
            "message": "Migration complete",
            "migrated": migrated,
            "skipped": skipped,
            "errors": errors,
            "total_in_storage": len(tasks),
        })
    except Exception as e:
        conn.rollback()
        print(f"[project_hub] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)
