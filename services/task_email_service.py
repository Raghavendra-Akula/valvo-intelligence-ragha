"""
task_email_service.py — Valvo-branded task notification emails.
Handles: assignment notifications, 1-day reminders, due-today alerts, overdue notices.
"""
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime


def _get_smtp_config():
    """Returns SMTP config or None if not configured."""
    email = os.getenv("SMTP_EMAIL", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if not email or not password:
        return None
    return {
        "email": email,
        "password": password,
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", 587)),
    }


def _send_email(to_email, subject, html_body, text_body=None):
    """Send an email via SMTP. Returns dict with success status."""
    cfg = _get_smtp_config()
    if not cfg:
        return {"success": False, "message": "SMTP not configured"}

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Valvo Intelligence <{cfg['email']}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
            server.starttls()
            server.login(cfg["email"], cfg["password"])
            server.send_message(msg)

        return {"success": True, "message": f"Sent to {to_email}"}
    except Exception as e:
        print(f"❌ Email send error: {e}")
        return {"success": False, "message": str(e)}


def _base_template(content, preheader=""):
    """Wraps content in the Valvo-branded email shell."""
    return f"""
    <div style="background:#0d1117;min-height:100vh;padding:0;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
      <div style="max-width:520px;margin:0 auto;padding:40px 20px;">

        <!-- Preheader (hidden preview text) -->
        <div style="display:none;max-height:0;overflow:hidden;">{preheader}</div>

        <!-- Logo -->
        <div style="text-align:center;margin-bottom:32px;">
          <div style="display:inline-block;background:linear-gradient(135deg,#4299e1,#63b3ed);padding:10px 18px;border-radius:12px;font-weight:900;font-size:22px;color:#0d1117;letter-spacing:1px;">V</div>
          <div style="margin-top:10px;font-size:22px;font-weight:800;color:#e2e8f0;letter-spacing:1px;">VALVO</div>
          <div style="font-size:10px;color:#4299e1;letter-spacing:3px;font-weight:600;">INTELLIGENCE</div>
        </div>

        <!-- Card -->
        <div style="background:#161b22;border:1px solid #21262d;border-radius:16px;padding:32px 28px;margin-bottom:24px;">
          {content}
        </div>

        <!-- Footer -->
        <div style="text-align:center;padding-top:16px;">
          <a href="https://app.valvointelligence.com/project-hub" style="display:inline-block;background:linear-gradient(135deg,#4299e1,#63b3ed);color:#fff;padding:12px 28px;border-radius:10px;font-weight:700;font-size:14px;text-decoration:none;margin-bottom:16px;">Open Project Hub</a>
          <p style="color:#484f58;font-size:11px;letter-spacing:2px;font-weight:600;margin-top:20px;">VALVO · VOLATILITY VOLUME VALUE</p>
        </div>
      </div>
    </div>
    """


def _format_date(date_str):
    """Format a date string or date object for display."""
    if not date_str:
        return "No due date"
    try:
        if isinstance(date_str, str):
            d = datetime.strptime(date_str, "%Y-%m-%d")
        else:
            d = date_str
        return d.strftime("%d %b %Y")
    except Exception:
        return str(date_str)


def _priority_badge(priority):
    """Returns styled HTML for priority."""
    colors = {
        "urgent": ("#ef5350", "#ef535020"),
        "high": ("#ffa726", "#ffa72620"),
        "medium": ("#29b6f6", "#29b6f620"),
        "low": ("#66bb6a", "#66bb6a20"),
    }
    color, bg = colors.get(priority, colors["medium"])
    return f'<span style="background:{bg};color:{color};padding:3px 10px;border-radius:6px;font-weight:700;font-size:11px;letter-spacing:0.5px;text-transform:uppercase;">{priority}</span>'


# ═══════════════════════════════════════════════════════════
# PUBLIC API — called by routes
# ═══════════════════════════════════════════════════════════

def send_task_assigned(to_email, to_name, task_title, task_description, due_date, priority, assigned_by):
    """Send 'you've been assigned a task' email."""
    due_str = _format_date(due_date)
    content = f"""
    <div style="font-size:13px;color:#8b949e;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">New Task Assigned</div>
    <div style="font-size:20px;font-weight:800;color:#e2e8f0;margin-bottom:16px;line-height:1.4;">{task_title}</div>

    {"<div style='color:#c9d1d9;font-size:14px;line-height:1.6;margin-bottom:20px;'>" + task_description + "</div>" if task_description else ""}

    <div style="border-top:1px solid #21262d;padding-top:16px;display:flex;flex-wrap:wrap;gap:16px;">
      <div>
        <div style="font-size:10px;color:#8b949e;letter-spacing:1px;font-weight:600;margin-bottom:4px;">ASSIGNED TO</div>
        <div style="font-size:14px;color:#e2e8f0;font-weight:700;">{to_name}</div>
      </div>
      <div>
        <div style="font-size:10px;color:#8b949e;letter-spacing:1px;font-weight:600;margin-bottom:4px;">DUE DATE</div>
        <div style="font-size:14px;color:#e2e8f0;font-weight:700;">{due_str}</div>
      </div>
      <div>
        <div style="font-size:10px;color:#8b949e;letter-spacing:1px;font-weight:600;margin-bottom:4px;">PRIORITY</div>
        <div>{_priority_badge(priority)}</div>
      </div>
      <div>
        <div style="font-size:10px;color:#8b949e;letter-spacing:1px;font-weight:600;margin-bottom:4px;">ASSIGNED BY</div>
        <div style="font-size:14px;color:#e2e8f0;font-weight:700;">{assigned_by}</div>
      </div>
    </div>
    """
    subject = f"📋 Task assigned: {task_title}"
    html = _base_template(content, preheader=f"You've been assigned: {task_title} — due {due_str}")
    return _send_email(to_email, subject, html)


def send_reminder_1day(to_email, to_name, task_title, due_date, priority):
    """Send '1 day before due' reminder."""
    due_str = _format_date(due_date)
    content = f"""
    <div style="font-size:13px;color:#ffa726;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">⏰ Due Tomorrow</div>
    <div style="font-size:20px;font-weight:800;color:#e2e8f0;margin-bottom:16px;line-height:1.4;">{task_title}</div>
    <div style="color:#c9d1d9;font-size:14px;margin-bottom:20px;">
      Hi {to_name}, this task is due <strong style="color:#ffa726;">tomorrow ({due_str})</strong>. Please make sure it's on track.
    </div>
    <div style="border-top:1px solid #21262d;padding-top:16px;">
      <span style="font-size:10px;color:#8b949e;letter-spacing:1px;font-weight:600;">PRIORITY</span>
      <span style="margin-left:8px;">{_priority_badge(priority)}</span>
    </div>
    """
    subject = f"⏰ Due tomorrow: {task_title}"
    html = _base_template(content, preheader=f"{task_title} is due tomorrow ({due_str})")
    return _send_email(to_email, subject, html)


def send_reminder_today(to_email, to_name, task_title, due_date, priority):
    """Send 'due today' reminder."""
    due_str = _format_date(due_date)
    content = f"""
    <div style="font-size:13px;color:#ef5350;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">🔴 Due Today</div>
    <div style="font-size:20px;font-weight:800;color:#e2e8f0;margin-bottom:16px;line-height:1.4;">{task_title}</div>
    <div style="color:#c9d1d9;font-size:14px;margin-bottom:20px;">
      Hi {to_name}, this task is due <strong style="color:#ef5350;">today ({due_str})</strong>. Time to wrap it up!
    </div>
    <div style="border-top:1px solid #21262d;padding-top:16px;">
      <span style="font-size:10px;color:#8b949e;letter-spacing:1px;font-weight:600;">PRIORITY</span>
      <span style="margin-left:8px;">{_priority_badge(priority)}</span>
    </div>
    """
    subject = f"🔴 Due today: {task_title}"
    html = _base_template(content, preheader=f"{task_title} is due today!")
    return _send_email(to_email, subject, html)


def send_overdue(to_email, to_name, task_title, due_date, priority, days_overdue):
    """Send 'overdue' alert."""
    due_str = _format_date(due_date)
    content = f"""
    <div style="font-size:13px;color:#ef5350;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">⚠️ Overdue — {days_overdue} day{"s" if days_overdue != 1 else ""}</div>
    <div style="font-size:20px;font-weight:800;color:#e2e8f0;margin-bottom:16px;line-height:1.4;">{task_title}</div>
    <div style="color:#c9d1d9;font-size:14px;margin-bottom:20px;">
      Hi {to_name}, this task was due on <strong style="color:#ef5350;">{due_str}</strong> and is now <strong>{days_overdue} day{"s" if days_overdue != 1 else ""} overdue</strong>. Please update its status or complete it.
    </div>
    <div style="border-top:1px solid #21262d;padding-top:16px;">
      <span style="font-size:10px;color:#8b949e;letter-spacing:1px;font-weight:600;">PRIORITY</span>
      <span style="margin-left:8px;">{_priority_badge(priority)}</span>
    </div>
    """
    subject = f"⚠️ Overdue ({days_overdue}d): {task_title}"
    html = _base_template(content, preheader=f"{task_title} is {days_overdue} days overdue")
    return _send_email(to_email, subject, html)
