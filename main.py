
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    current_app,
)
from . import db, limiter, csrf
from .auth import login_required, current_user
from .models import EmailMessage, NotificationLog, AuditLog, User, BlockedSender, TrustedSender, EmailTask, EmailAttachment
from email.utils import parseaddr
from .tasks import monitor_user, whatsapp_task
from .services.email_service import send_gmail_reply


main_bp = Blueprint("main", __name__)

MAX_PROFILE_IMAGE_SIZE = 10 * 1024 * 1024


@main_bp.get("/")
def index():
    return redirect(
        url_for("main.dashboard") if current_user()
        else url_for("auth.login")
    )


@main_bp.get("/privacy")
def privacy_policy():
    """Public privacy policy page required for Google OAuth verification & compliance."""
    return render_template("privacy.html")


@main_bp.get("/dashboard")
@login_required
def dashboard():
    user = current_user()
    view = (request.args.get("view") or "recent").strip().lower()
    search_query = (request.args.get("q") or "").strip()[:200]

    base_query = EmailMessage.query.filter_by(
        user_id=user.id, is_trashed=False
    )
    if view == "spam":
        email_query = base_query.filter(EmailMessage.is_spam.is_(True))
    elif view == "suspicious":
        email_query = base_query.filter(EmailMessage.phishing_score >= 0.45, EmailMessage.is_spam.is_(False))
    elif view == "all":
        email_query = base_query
    elif view == "inbox":
        email_query = base_query.filter(
            EmailMessage.is_spam.is_(False), EmailMessage.phishing_score < 0.45,
            db.or_(EmailMessage.gmail_labels.is_(None), EmailMessage.gmail_labels.notilike("%SENT%")),
            db.or_(EmailMessage.gmail_labels.is_(None), EmailMessage.gmail_labels.notilike("%DRAFT%")),
        )
    elif view in {"sent", "drafts", "promotions", "social", "updates"}:
        label = {"sent": "SENT", "drafts": "DRAFT", "promotions": "CATEGORY_PROMOTIONS", "social": "CATEGORY_SOCIAL", "updates": "CATEGORY_UPDATES"}[view]
        email_query = base_query.filter(EmailMessage.gmail_labels.ilike(f"%{label}%"))
    else:
        email_query = base_query
    important_query = base_query.filter(
        EmailMessage.is_spam.is_(False),
        EmailMessage.phishing_score < 0.45,
        db.or_(
            EmailMessage.priority.in_(["Important", "Urgent"]),
            EmailMessage.category.ilike("important"),
        )
    )

    # Keep all mailbox views scoped to the authenticated user.
    if view == "important":
        email_query = important_query
    elif view not in {"recent", "spam", "suspicious", "all", "inbox", "sent", "drafts", "promotions", "social", "updates"}:
        view = "recent"
        email_query = base_query
    if search_query:
        pattern = f"%{search_query}%"
        email_query = email_query.filter(db.or_(
            EmailMessage.sender.ilike(pattern), EmailMessage.subject.ilike(pattern),
            EmailMessage.body.ilike(pattern), EmailMessage.summary.ilike(pattern),
            EmailMessage.category.ilike(pattern),
        ))
    page = max(1, request.args.get("page", 1, type=int) or 1)
    pagination = email_query.order_by(EmailMessage.received_at.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    emails = pagination.items

    notifications = (
        NotificationLog.query.filter_by(user_id=user.id)
        .order_by(NotificationLog.created_at.desc())
        .limit(20)
        .all()
    )

    stats = {
        "emails": base_query.count(),
        "alerts": NotificationLog.query.filter_by(user_id=user.id).count(),
        "confidential": base_query.filter(
            EmailMessage.confidentiality.in_(
                ["Confidential", "Highly Confidential"]
            ),
        ).count(),
        # Count all important and urgent messages using the same rule as the filter.
        "urgent": important_query.count(),
        "spam": base_query.filter(EmailMessage.is_spam.is_(True)).count(),
        "suspicious": base_query.filter(EmailMessage.phishing_score >= 0.45, EmailMessage.is_spam.is_(False)).count(),
        "tasks": EmailTask.query.filter(EmailTask.user_id == user.id, EmailTask.status.in_(["open", "suggested"])).count(),
        "attachments": EmailAttachment.query.filter_by(user_id=user.id).count(),
    }

    hour = datetime.now().hour
    if 5 <= hour < 12:
        greeting = "Good Morning"
    elif 12 <= hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"

    return render_template(
        "dashboard.html",
        user=user,
        greeting=greeting,
        emails=emails,
        pagination=pagination,
        notifications=notifications,
        stats=stats,
        view=view,
        search_query=search_query,
        blocked_senders=BlockedSender.query.filter_by(user_id=user.id).order_by(BlockedSender.created_at.desc()).limit(20).all(),
    )


def _is_html_content(content):
    if not content:
        return False
    lower = content.lower()
    return any(
        marker in lower
        for marker in (
            "<html", "<div", "<p>", "<p ", "<table", "<font", "<span",
            "<br>", "<br/>", "<br />", "<body", "<head", "<strong", "<em",
            "<b ", "<b>", "<i ", "<i>", "<tr", "<td", "<ul", "<ol", "<li"
        )
    )


def _sanitize_html_body(html_content):
    if not html_content:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "noscript", "iframe", "object", "embed", "applet"]):
            tag.decompose()
        for tag in soup.find_all(True):
            attrs = dict(tag.attrs)
            for attr in attrs:
                if attr.lower().startswith("on") or (isinstance(attrs[attr], str) and "javascript:" in attrs[attr].lower()):
                    del tag.attrs[attr]
        for a_tag in soup.find_all("a"):
            href = a_tag.get("href", "")
            if isinstance(href, str):
                href_clean = href.strip()
                if href_clean.startswith("www."):
                    a_tag["href"] = "https://" + href_clean
                elif href_clean.lower().startswith("javascript:"):
                    a_tag["href"] = "#"
            a_tag["target"] = "_blank"
            a_tag["rel"] = "noopener noreferrer"
        return str(soup)
    except Exception:
        return html_content


def _html_to_plain_text(content):
    if not content or "<" not in content:
        return content or ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", content).strip()


@main_bp.get("/email/<int:email_id>")
@login_required
def view_email(email_id):
    user = current_user()
    row = EmailMessage.query.filter_by(id=email_id, user_id=user.id).first_or_404()
    if not row.is_read:
        row.is_read = True
        db.session.commit()
    is_blocked = False
    is_trusted = False
    if row.sender:
        _, sender_addr = parseaddr(row.sender)
        sender_addr = sender_addr.strip().lower()
        if sender_addr:
            is_blocked = BlockedSender.query.filter_by(user_id=user.id, sender_key=sender_addr).first() is not None
            is_trusted = TrustedSender.query.filter_by(user_id=user.id, sender_key=sender_addr).first() is not None

    body_raw = row.body or ""
    is_html = _is_html_content(body_raw)
    sanitized_html = _sanitize_html_body(body_raw) if is_html else ""
    plain_text_body = _html_to_plain_text(body_raw) if is_html else body_raw
    clean_summary = _html_to_plain_text(row.summary or "")

    # Ensure AI contextual reply is pre-drafted if missing
    if not row.ai_reply and not row.is_spam and (row.phishing_score or 0) < 0.45:
        try:
            from .services.reply_generator import generate_ai_reply
            row.ai_reply = generate_ai_reply(row, user)
            db.session.commit()
        except Exception:
            pass

    auto_reply_status = "disabled"
    auto_reply_eta = None
    if getattr(user, "auto_reply_enabled", False):
        if row.auto_reply_sent_at:
            auto_reply_status = "sent"
        elif row.status == "replied":
            auto_reply_status = "manual_replied"
        elif row.is_spam or row.is_trashed:
            auto_reply_status = "suppressed"
        else:
            delay_h = getattr(user, "auto_reply_delay_hours", 6) or 6
            ref_time = row.received_at
            if not ref_time:
                ref_time = datetime.now(timezone.utc)
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            due_at = ref_time + timedelta(hours=delay_h)
            now_utc = datetime.now(timezone.utc)
            sec_left = int((due_at - now_utc).total_seconds())
            if sec_left <= 0:
                auto_reply_status = "dispatching_soon"
                auto_reply_eta = "Due now (queued for automated sweep)"
            else:
                auto_reply_status = "scheduled"
                h_left = sec_left // 3600
                m_left = (sec_left % 3600) // 60
                auto_reply_eta = f"In {h_left}h {m_left}m"

    return render_template(
        "email_detail.html",
        user=user,
        e=row,
        attachments=row.attachments if hasattr(row, "attachments") else [],
        is_blocked=is_blocked,
        is_trusted=is_trusted,
        is_html=is_html,
        sanitized_html=sanitized_html,
        plain_text_body=plain_text_body,
        clean_summary=clean_summary,
        auto_reply_status=auto_reply_status,
        auto_reply_eta=auto_reply_eta,
    )


@main_bp.get("/api/emails/<int:email_id>")
@login_required
def get_email_json(email_id):
    user = current_user()
    row = EmailMessage.query.filter_by(id=email_id, user_id=user.id).first_or_404()
    if not row.is_read:
        row.is_read = True
        db.session.commit()

    body_raw = row.body or ""
    is_html = _is_html_content(body_raw)
    sanitized_html = _sanitize_html_body(body_raw) if is_html else ""
    plain_text_body = _html_to_plain_text(body_raw) if is_html else body_raw
    clean_summary = _html_to_plain_text(row.summary or "")

    att_list = []
    for att in getattr(row, "attachments", []):
        att_list.append({
            "id": att.id,
            "filename": att.filename,
            "mime_type": att.mime_type or "Unknown",
            "size_kb": round((att.size_bytes or 0) / 1024, 1),
            "extracted_text": (att.extracted_text or "")[:4000],
        })

    return jsonify({
        "id": row.id,
        "subject": row.subject or "(No subject)",
        "sender": row.sender or "Unknown sender",
        "recipient": row.recipient or (getattr(user, "email", "") or ""),
        "received_at": row.received_at.strftime("%a, %d %b %Y · %I:%M %p") if row.received_at else "",
        "category": row.category or "",
        "priority": row.priority or "Normal",
        "confidentiality": row.confidentiality or "General",
        "is_spam": bool(row.is_spam),
        "spam_score": round((row.spam_score or 0) * 100),
        "spam_reason": row.spam_reason or "",
        "phishing_score": round((row.phishing_score or 0) * 100),
        "phishing_reason": row.phishing_reason or "",
        "summary": clean_summary or row.summary or "",
        "is_html": is_html,
        "body_html": sanitized_html,
        "body_plain": plain_text_body,
        "attachments": att_list,
        "view_url": url_for("main.view_email", email_id=row.id),
        "reply_url": url_for("main.view_email", email_id=row.id) + "#reply-section",
    })


@main_bp.post("/email/<int:email_id>/spam")
@login_required
def mark_spam(email_id):
    user = current_user()
    row = EmailMessage.query.filter_by(id=email_id, user_id=user.id, is_trashed=False).first_or_404()
    row.is_spam = True
    row.user_override = "spam"
    row.spam_score = max(float(row.spam_score or 0), 1.0)
    row.spam_reason = "Marked as spam by user"
    db.session.add(AuditLog(user_id=user.id, action="email_marked_spam", details=f"email_id={row.id}"))
    db.session.commit()
    flash("Email moved to NEXORA Spam & Quarantine; future monitoring scans will suppress new alerts for it.", "success")
    return redirect(request.form.get("next") or request.referrer or url_for("main.dashboard", view="spam"))


@main_bp.post("/email/<int:email_id>/not-spam")
@login_required
def mark_not_spam(email_id):
    user = current_user()
    row = EmailMessage.query.filter_by(id=email_id, user_id=user.id, is_trashed=False).first_or_404()
    row.is_spam = False
    row.user_override = "not_spam"
    row.spam_score = min(float(row.spam_score or 0), 0.15)
    row.spam_reason = "Marked as legitimate by user"
    db.session.add(AuditLog(user_id=user.id, action="email_marked_not_spam", details=f"email_id={row.id}"))
    db.session.commit()
    flash("Email restored to your normal mailbox view.", "success")
    return redirect(request.form.get("next") or request.referrer or url_for("main.dashboard", view="inbox"))


@main_bp.post("/email/<int:email_id>/block-sender")
@login_required
def block_sender(email_id):
    user = current_user()
    row = EmailMessage.query.filter_by(id=email_id, user_id=user.id, is_trashed=False).first_or_404()
    _, address = parseaddr(row.sender or "")
    address = address.strip().lower()
    match_type = request.form.get("match_type", "email").lower()
    if match_type not in {"email", "domain"} or "@" not in address:
        flash("This sender does not contain a valid email address.", "warning")
        return redirect(url_for("main.dashboard"))
    key = address if match_type == "email" else address.rsplit("@", 1)[-1]
    existing = BlockedSender.query.filter_by(user_id=user.id, sender_key=key, match_type=match_type).first()
    if not existing:
        db.session.add(BlockedSender(user_id=user.id, sender_key=key, match_type=match_type, reason="Blocked from email dashboard"))
    rows = EmailMessage.query.filter_by(user_id=user.id, is_trashed=False).filter(EmailMessage.sender.ilike(f"%{key}%")).all()
    for email in rows:
        email.is_spam = True
        email.user_override = "spam"
        email.spam_score = 1.0
        email.spam_reason = f"Sender {match_type} is on your blocklist"
    db.session.add(AuditLog(user_id=user.id, action="sender_blocked", details=f"match_type={match_type}; key={key}"))
    db.session.commit()
    flash(f"Blocked {match_type}: {key}. Matching messages are quarantined in NEXORA.", "success")
    return redirect(url_for("main.dashboard", view="spam"))


@main_bp.post("/senders/<int:sender_id>/unblock")
@login_required
def unblock_sender(sender_id):
    user = current_user()
    row = BlockedSender.query.filter_by(id=sender_id, user_id=user.id).first_or_404()
    key, match_type = row.sender_key, row.match_type
    db.session.delete(row)
    db.session.add(AuditLog(user_id=user.id, action="sender_unblocked", details=f"match_type={match_type}; key={key}"))
    db.session.commit()
    flash(f"Removed {key} from your blocklist. Previously quarantined messages remain in Spam until restored.", "success")
    return redirect(url_for("main.dashboard", view="spam"))


@main_bp.post("/email/<int:email_id>/trust-sender")
@login_required
def trust_sender(email_id):
    user = current_user()
    row = EmailMessage.query.filter_by(id=email_id, user_id=user.id, is_trashed=False).first_or_404()
    _, address = parseaddr(row.sender or "")
    address = address.strip().lower()
    if "@" not in address:
        flash("Cannot trust this sender because its email address is unavailable.", "warning")
        return redirect(url_for("main.dashboard"))
    blocked = BlockedSender.query.filter_by(user_id=user.id, sender_key=address, match_type="email").first()
    if blocked: db.session.delete(blocked)
    if not TrustedSender.query.filter_by(user_id=user.id, sender_key=address, match_type="email").first():
        db.session.add(TrustedSender(user_id=user.id, sender_key=address, match_type="email"))
    row.is_spam = False; row.user_override = "not_spam"; row.spam_reason = "Sender trusted by user"
    db.session.commit()
    flash(f"Trusted sender {address}.", "success")
    return redirect(url_for("main.dashboard", view="inbox"))


@main_bp.get("/analytics")
@login_required
def analytics():
    user = current_user()
    base = EmailMessage.query.filter_by(user_id=user.id, is_trashed=False)
    total_stored = base.count()

    # 1. Important Mail
    important_count = base.filter(
        db.or_(
            EmailMessage.priority.in_(["Important", "Urgent"]),
            EmailMessage.category.ilike("%important%")
        )
    ).count()

    # 2. Work Mail
    work_count = base.filter(
        db.or_(
            EmailMessage.category.ilike("%work%"),
            EmailMessage.category.ilike("%office%"),
            EmailMessage.category.ilike("%project%")
        )
    ).count()

    # 3. Spam Mail
    spam_count = base.filter(
        db.or_(
            EmailMessage.category.ilike("%spam%"),
            EmailMessage.category.ilike("%promo%"),
            EmailMessage.spam_score >= 0.5
        )
    ).count()

    # 4. Quarantined Spam
    quarantined_count = base.filter_by(is_spam=True).count()

    # 5. Suspicious Signals
    suspicious_count = base.filter(EmailMessage.phishing_score >= 0.45).count()

    # Compute percentages against total stored emails
    if total_stored > 0:
        important_pct = round((important_count / total_stored) * 100, 1)
        work_pct = round((work_count / total_stored) * 100, 1)
        spam_pct = round((spam_count / total_stored) * 100, 1)
        quarantined_pct = round((quarantined_count / total_stored) * 100, 1)
        suspicious_pct = round((suspicious_count / total_stored) * 100, 1)
    else:
        important_pct = 0.0
        work_pct = 0.0
        spam_pct = 0.0
        quarantined_pct = 0.0
        suspicious_pct = 0.0

    pie_data = [
        {
            "label": "Important Mail",
            "count": important_count,
            "pct": important_pct,
            "color": "#f59e0b",
            "icon": "★",
            "description": "High-priority / urgent correspondence",
        },
        {
            "label": "Work Mail",
            "count": work_count,
            "pct": work_pct,
            "color": "#0284c7",
            "icon": "💼",
            "description": "Professional, project, and workplace emails",
        },
        {
            "label": "Spam",
            "count": spam_count,
            "pct": spam_pct,
            "color": "#f97316",
            "icon": "📢",
            "description": "Promotional offers and general spam",
        },
        {
            "label": "Quarantined Spam",
            "count": quarantined_count,
            "pct": quarantined_pct,
            "color": "#ef4444",
            "icon": "⛨",
            "description": "Isolated and quarantined messages",
        },
        {
            "label": "Suspicious Signals",
            "count": suspicious_count,
            "pct": suspicious_pct,
            "color": "#a855f7",
            "icon": "⚠",
            "description": "Phishing threats and anomaly alerts",
        },
    ]

    categories = (
        db.session.query(EmailMessage.category, db.func.count(EmailMessage.id))
        .filter_by(user_id=user.id, is_trashed=False)
        .group_by(EmailMessage.category)
        .order_by(db.func.count(EmailMessage.id).desc())
        .all()
    )
    notifications = NotificationLog.query.filter_by(user_id=user.id).all()
    total = len(notifications)
    successful = sum(1 for n in notifications if (n.status or "").lower() in {"sent", "delivered", "queued", "accepted"})

    return render_template(
        "analytics.html",
        user=user,
        total=total_stored,
        spam=quarantined_count,
        suspicious=suspicious_count,
        categories=categories,
        notification_total=total,
        notification_success=successful,
        notification_rate=(successful / total * 100 if total else 0),
        important_count=important_count,
        important_pct=important_pct,
        work_count=work_count,
        work_pct=work_pct,
        spam_count=spam_count,
        spam_pct=spam_pct,
        quarantined_count=quarantined_count,
        quarantined_pct=quarantined_pct,
        suspicious_count=suspicious_count,
        suspicious_pct=suspicious_pct,
        pie_data=pie_data,
    )


@main_bp.get("/tasks")
@login_required
def tasks_page():
    user = current_user()
    tasks = (
        EmailTask.query.options(db.joinedload(EmailTask.email))
        .filter_by(user_id=user.id)
        .order_by(EmailTask.due_at.asc().nullslast(), EmailTask.created_at.desc())
        .limit(300)
        .all()
    )
    return render_template("tasks.html", user=user, tasks=tasks)


@main_bp.post("/tasks/<int:task_id>/status")
@login_required
def task_status(task_id):
    user = current_user()
    task = EmailTask.query.filter_by(id=task_id, user_id=user.id).first_or_404()
    status = request.form.get("status", "open")
    if status not in {"open", "done", "dismissed"}:
        flash("Invalid task status.", "warning")
    else:
        task.status = status
        db.session.commit()
        flash("Task status updated.", "success")
    return redirect(url_for("main.tasks_page"))


@main_bp.get("/trash")
@login_required
def trash():
    user = current_user()

    trashed_emails = (
        EmailMessage.query
        .filter_by(user_id=user.id, is_trashed=True)
        .order_by(EmailMessage.received_at.desc())
        .all()
    )

    return render_template(
        "trash.html",
        user=user,
        emails=trashed_emails,
    )


@main_bp.post("/email/<int:email_id>/trash")
@login_required
def move_email_to_trash(email_id):
    user = current_user()

    email = EmailMessage.query.filter_by(
        id=email_id,
        user_id=user.id,
        is_trashed=False,
    ).first_or_404()

    try:
        email.is_trashed = True
        db.session.commit()
        flash("Email moved to Trash.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not move email to Trash.")
        flash("Could not move the email to Trash.", "danger")

    return redirect(request.form.get("next") or request.referrer or url_for("main.dashboard"))


@main_bp.post("/email/<int:email_id>/restore")
@login_required
def restore_email(email_id):
    user = current_user()

    email = EmailMessage.query.filter_by(
        id=email_id,
        user_id=user.id,
        is_trashed=True,
    ).first_or_404()

    try:
        email.is_trashed = False
        db.session.commit()
        flash("Email restored to the dashboard.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not restore email.")
        flash("Could not restore the email.", "danger")

    return redirect(url_for("main.trash"))


@main_bp.post("/email/<int:email_id>/delete-permanently")
@login_required
def delete_email_permanently(email_id):
    user = current_user()

    email = EmailMessage.query.filter_by(
        id=email_id,
        user_id=user.id,
        is_trashed=True,
    ).first_or_404()

    try:
        db.session.delete(email)
        db.session.commit()
        flash("Email permanently deleted from NEXORA.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not permanently delete email.")
        flash("Could not permanently delete the email.", "danger")

    return redirect(url_for("main.trash"))


@main_bp.post("/trash/empty")
@login_required
def empty_trash():
    user = current_user()
    try:
        trashed_emails = EmailMessage.query.filter_by(
            user_id=user.id,
            is_trashed=True,
        ).all()
        count = len(trashed_emails)
        if count == 0:
            flash("Trash is already empty.", "info")
            return redirect(url_for("main.trash"))

        for email in trashed_emails:
            db.session.delete(email)
        db.session.commit()
        flash(f"Trash emptied successfully. {count} email(s) permanently deleted.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not empty trash.")
        flash("Could not empty trash. Please try again.", "danger")

    return redirect(url_for("main.trash"))


@main_bp.post("/trash/restore-all")
@login_required
def restore_all_trash():
    user = current_user()
    try:
        trashed_emails = EmailMessage.query.filter_by(
            user_id=user.id,
            is_trashed=True,
        ).all()
        count = len(trashed_emails)
        if count == 0:
            flash("No emails in Trash to restore.", "info")
            return redirect(url_for("main.trash"))

        for email in trashed_emails:
            email.is_trashed = False
        db.session.commit()
        flash(f"All {count} email(s) have been restored to your mailbox.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not restore all emails from trash.")
        flash("Could not restore emails. Please try again.", "danger")

    return redirect(url_for("main.trash"))


@main_bp.post("/trash/restore-selected")
@login_required
def restore_selected_trash():
    user = current_user()
    selected_ids = request.form.getlist("selected_ids")
    valid_ids = []
    for sid in selected_ids:
        try:
            valid_ids.append(int(sid))
        except (ValueError, TypeError):
            continue

    if not valid_ids:
        flash("No emails were selected to restore.", "warning")
        return redirect(url_for("main.trash"))

    try:
        trashed_emails = EmailMessage.query.filter(
            EmailMessage.id.in_(valid_ids),
            EmailMessage.user_id == user.id,
            EmailMessage.is_trashed == True,
        ).all()
        count = len(trashed_emails)
        if count == 0:
            flash("No matching trashed emails found.", "warning")
            return redirect(url_for("main.trash"))

        for email in trashed_emails:
            email.is_trashed = False
        db.session.commit()
        flash(f"Restored {count} selected email(s) to your mailbox.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not restore selected emails.")
        flash("Could not restore selected emails. Please try again.", "danger")

    return redirect(url_for("main.trash"))


@main_bp.post("/trash/delete-selected")
@login_required
def delete_selected_trash():
    user = current_user()
    selected_ids = request.form.getlist("selected_ids")
    valid_ids = []
    for sid in selected_ids:
        try:
            valid_ids.append(int(sid))
        except (ValueError, TypeError):
            continue

    if not valid_ids:
        flash("No emails were selected to delete.", "warning")
        return redirect(url_for("main.trash"))

    try:
        trashed_emails = EmailMessage.query.filter(
            EmailMessage.id.in_(valid_ids),
            EmailMessage.user_id == user.id,
            EmailMessage.is_trashed == True,
        ).all()
        count = len(trashed_emails)
        if count == 0:
            flash("No matching trashed emails found.", "warning")
            return redirect(url_for("main.trash"))

        for email in trashed_emails:
            db.session.delete(email)
        db.session.commit()
        flash(f"Permanently deleted {count} selected email(s).", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not delete selected emails.")
        flash("Could not delete selected emails. Please try again.", "danger")

    return redirect(url_for("main.trash"))


@main_bp.post("/monitor")
@login_required
@limiter.limit("20 per minute")
def monitor():
    user = current_user()
    if not user.google_sub:
        flash("Connect Google before scanning Gmail.", "warning")
        return redirect(url_for("main.dashboard"))

    try:
        task = monitor_user.delay(user.id)
        flash(f"Gmail scan queued • {task.id[:8]}", "info")
    except Exception as exc:
        current_app.logger.info("Celery broker unavailable, falling back to direct inbox scan: %s", exc)
        try:
            from .services.email_service import monitor_inbox
            count = monitor_inbox(user)
            flash(f"Gmail scan complete • {count} new message(s) imported.", "success")
        except Exception as scan_exc:
            current_app.logger.exception("Direct Gmail monitoring failed")
            flash(f"Monitoring could not be performed: {str(scan_exc)}", "danger")

    return redirect(url_for("main.dashboard"))


@main_bp.get("/api/monitor/live-poll")
@login_required
def monitor_live_poll():
    """Live dashboard poller that delivers newly imported emails in real-time."""
    user = current_user()
    since_id = request.args.get("since_id", 0, type=int)
    auto_scan = request.args.get("scan", "1").lower() in {"1", "true", "yes"}

    new_scanned_count = 0
    if user.monitoring_enabled and user.google_sub and auto_scan:
        try:
            from .services.continuous_monitor import scan_user_inbox_safe
            new_scanned_count = scan_user_inbox_safe(
                user.id, current_app._get_current_object(), min_interval=12.0
            )
        except Exception as exc:
            current_app.logger.warning("Live poll scan failed: %s", exc)

    base_query = EmailMessage.query.filter_by(user_id=user.id, is_trashed=False)
    total_emails = base_query.count()
    unread_emails = base_query.filter(EmailMessage.is_read.is_(False)).count()
    urgent_count = base_query.filter(
        EmailMessage.is_spam.is_(False),
        EmailMessage.phishing_score < 0.45,
        db.or_(
            EmailMessage.priority.in_(["Important", "Urgent"]),
            EmailMessage.category.ilike("important"),
        ),
    ).count()

    latest_msg = base_query.order_by(EmailMessage.id.desc()).first()
    latest_id = latest_msg.id if latest_msg else 0

    new_incoming = []
    if since_id > 0:
        incoming_rows = (
            base_query.filter(EmailMessage.id > since_id)
            .order_by(EmailMessage.id.desc())
            .limit(20)
            .all()
        )
        for r in incoming_rows:
            new_incoming.append({
                "id": r.id,
                "sender": r.sender or "Unknown sender",
                "subject": r.subject or "(No subject)",
                "summary": r.summary or "No summary available.",
                "category": r.category or "General",
                "priority": r.priority or "Normal",
                "confidentiality": r.confidentiality or "General",
                "received_at": r.received_at.strftime("%I:%M %p") if r.received_at else "Just now",
                "is_read": bool(r.is_read),
                "is_spam": bool(r.is_spam),
                "spam_score": float(r.spam_score or 0.0),
                "phishing_score": float(r.phishing_score or 0.0),
                "has_attachments": bool(r.has_attachments),
                "view_url": url_for("main.view_email", email_id=r.id),
                "reply_url": url_for("main.view_email", email_id=r.id) + "#reply-section",
                "avatar_letter": ((r.sender or "?")[:1]).upper(),
            })

    return jsonify({
        "ok": True,
        "monitoring_enabled": bool(user.monitoring_enabled),
        "new_scanned_count": new_scanned_count,
        "total_emails": total_emails,
        "unread_emails": unread_emails,
        "urgent_count": urgent_count,
        "latest_id": latest_id,
        "new_incoming": new_incoming,
    })


@main_bp.post("/monitor/toggle")
@login_required
def monitor_toggle():
    user = current_user()
    action = request.form.get("action", "start")

    if action == "start" and not user.google_sub:
        flash("Connect Google before enabling automatic monitoring.", "warning")
        return redirect(url_for("main.dashboard"))

    user.monitoring_enabled = action == "start"

    db.session.add(
        AuditLog(
            user_id=user.id,
            action="monitoring_changed",
            details="enabled" if user.monitoring_enabled else "disabled",
        )
    )
    db.session.commit()

    flash(
        "Automatic Gmail monitoring is now ON."
        if user.monitoring_enabled
        else "Automatic Gmail monitoring is now OFF.",
        "success",
    )
    return redirect(url_for("main.dashboard"))


@main_bp.post("/email/<int:email_id>/reply")
@login_required
@limiter.limit("20 per minute")
def reply(email_id):
    user = current_user()

    row = EmailMessage.query.filter_by(
        id=email_id, user_id=user.id
    ).first_or_404()

    text = (request.form.get("reply") or "").strip()

    redirect_target = request.form.get("next") or request.referrer or url_for("main.dashboard")

    if not text:
        flash("Reply cannot be empty.", "danger")
        return redirect(redirect_target)

    uploaded_files = request.files.getlist("attachments")
    valid_attachments = [f for f in uploaded_files if f and f.filename and f.filename.strip()]

    # Validate attachment size limit (15 MB total)
    max_attachment_bytes = 15 * 1024 * 1024
    total_attachment_size = 0
    for f in valid_attachments:
        f.seek(0, os.SEEK_END)
        total_attachment_size += f.tell()
        f.seek(0)

    if total_attachment_size > max_attachment_bytes:
        flash("Total attachment size exceeds the 15 MB limit. Please select smaller files.", "danger")
        return redirect(redirect_target)

    try:
        send_gmail_reply(user, row.message_id, text, attachments=valid_attachments)
        row.ai_reply = text
        row.status = "replied"

        db.session.add(
            AuditLog(
                user_id=user.id,
                action="gmail_reply_sent",
                details=f"email_id={row.id}, attachments={len(valid_attachments)}",
            )
        )
        db.session.commit()

        if user.whatsapp_number:
            whatsapp_task.delay(
                user.id,
                f"NEXORA • Reply sent\n"
                f"Subject: {row.subject or 'Email'}\n"
                f"Status: Gmail reply delivered{' with ' + str(len(valid_attachments)) + ' attachment(s)' if valid_attachments else ''}.",
            )

        attach_msg = f" with {len(valid_attachments)} attachment(s)" if valid_attachments else ""
        flash(f"Reply sent successfully via Gmail{attach_msg}.", "success")

    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Gmail reply failed")
        flash("Reply failed. Check the Google connection and server logs, then try again.", "danger")

    return redirect(redirect_target)


@main_bp.post("/auto-reply/toggle")
@login_required
def toggle_auto_reply():
    user = current_user()
    user.auto_reply_enabled = not user.auto_reply_enabled
    db.session.add(
        AuditLog(
            user_id=user.id,
            action="auto_reply_toggle",
            details=f"enabled={user.auto_reply_enabled}",
        )
    )
    db.session.commit()
    if user.auto_reply_enabled:
        flash("Auto AI Reply System is now ENABLED. NEXORA will intelligently draft and send contextual replies after 6 hours if an email remains unreplied.", "success")
        try:
            from .services.auto_reply_service import process_auto_ai_replies
            process_auto_ai_replies(user.id)
        except Exception:
            pass
    else:
        flash("Auto AI Reply System is now DISABLED. Automatic email replies will not be sent.", "warning")
    return redirect(request.form.get("next") or request.referrer or url_for("main.dashboard"))


@main_bp.route("/mfa/toggle", methods=["GET", "POST"])
@login_required
def toggle_mfa():
    """Toggle 2-Step Verification directly from the dashboard or navigate to Google Account 2-Step Verification."""
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action in {"start", "stop"}:
            enable = (action == "start")
        else:
            enable = not user.mfa_enabled
    else:
        action = request.args.get("action")
        if action in {"start", "stop"}:
            enable = (action == "start")
        else:
            enable = not user.mfa_enabled

    user.mfa_enabled = enable
    if enable and not user.mfa_secret:
        import pyotp
        user.mfa_secret = pyotp.random_base32()

    db.session.add(
        AuditLog(
            user_id=user.id,
            action="mfa_enabled" if enable else "mfa_disabled",
            details="dashboard_toggle",
        )
    )
    db.session.commit()

    redirect_to_google = request.values.get("redirect_to_google") in {"1", "true", "yes"}

    if enable:
        flash("2-Step Verification is now ON. You can manage Google Account 2-Step Verification directly.", "success")
        if redirect_to_google:
            return redirect("https://myaccount.google.com/signinoptions/two-step-verification")
    else:
        flash("2-Step Verification is now OFF.", "warning")

    return redirect(request.values.get("next") or request.referrer or url_for("main.dashboard"))


@main_bp.get("/mfa/google")
@login_required
def open_google_mfa():
    """Direct shortcut into Google Account 2-Step Verification management."""
    return redirect("https://myaccount.google.com/signinoptions/two-step-verification")


@main_bp.post("/email/<int:email_id>/regenerate-ai-reply")
@login_required
def regenerate_ai_reply(email_id):
    user = current_user()
    row = EmailMessage.query.filter_by(id=email_id, user_id=user.id).first_or_404()
    try:
        from .services.reply_generator import generate_ai_reply
        row.ai_reply = generate_ai_reply(row, user)
        db.session.commit()
        flash("Contextual AI reply regenerated based on full email content.", "success")
    except Exception as exc:
        db.session.rollback()
        flash("Could not regenerate AI reply: " + str(exc), "danger")
    return redirect(url_for("main.view_email", email_id=row.id) + "#reply-section")


@main_bp.post("/whatsapp")
@login_required
def whatsapp_settings():
    number = request.form.get("whatsapp_number", "").strip()
    user = current_user()

    if not number:
        flash("Please enter a WhatsApp number.", "danger")
        return redirect(url_for("main.dashboard") + "#whatsapp")

    try:
        from .services.whatsapp import normalize_whatsapp_number
        clean_num = normalize_whatsapp_number(number)
    except ValueError as ve:
        flash(str(ve), "danger")
        return redirect(url_for("main.dashboard") + "#whatsapp")

    user.whatsapp_number = clean_num
    db.session.add(
        AuditLog(user_id=user.id, action="whatsapp_number_updated", details=f"number={clean_num}")
    )

    try:
        db.session.commit()
        flash(f"WhatsApp destination saved: {clean_num}. If you haven't yet, scan the Twilio Sandbox QR code below to activate alerts.", "success")
    except Exception:
        db.session.rollback()
        flash("Unable to save the WhatsApp destination.", "danger")

    return redirect(url_for("main.dashboard") + "#whatsapp")


@main_bp.post("/whatsapp/test")
@login_required
@limiter.limit("10 per minute")
def whatsapp_test():
    user = current_user()

    if not user.whatsapp_number:
        flash("Save your WhatsApp number first before sending a test alert.", "warning")
        return redirect(url_for("main.dashboard") + "#whatsapp")

    test_message = (
        "NEXORA • TEST ALERT\n"
        "Your WhatsApp notification channel is connected successfully to the Twilio Sandbox.\n"
        "You will receive high-priority email alerts and updates here."
    )

    try:
        from .services.whatsapp import send_whatsapp
        result = send_whatsapp(user.whatsapp_number, test_message)

        if result.get("ok"):
            log = NotificationLog(
                user_id=user.id,
                channel="WhatsApp",
                destination=user.whatsapp_number,
                message=test_message,
                status=result.get("status", "sent"),
                provider_id=result.get("sid"),
            )
            db.session.add(log)
            db.session.commit()
            flash(f"Test WhatsApp alert sent successfully to {user.whatsapp_number}! Check your phone.", "success")
        else:
            err = result.get("error", "Twilio delivery failed.")
            flash(f"Twilio Sandbox Alert Failed: {err}. Please scan the QR code and send 'join refused-sea' in WhatsApp first.", "danger")

    except Exception as exc:
        current_app.logger.exception("Could not send test WhatsApp alert")
        flash(f"Could not send the test alert: {str(exc)}", "danger")

    return redirect(url_for("main.dashboard") + "#whatsapp")


@main_bp.post("/webhooks/twilio/status")
@csrf.exempt
def twilio_status_webhook():
    """Update delivery state only after validating Twilio's request signature."""
    callback_url = (current_app.config.get("TWILIO_STATUS_CALLBACK_URL") or "").strip()
    auth_token = (current_app.config.get("TWILIO_AUTH_TOKEN") or "").strip()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not callback_url or not auth_token or not signature:
        return jsonify({"error": "Webhook signature validation is not configured."}), 403
    try:
        from twilio.request_validator import RequestValidator
        valid = RequestValidator(auth_token).validate(callback_url, request.form, signature)
    except Exception:
        current_app.logger.exception("Twilio webhook signature validation failed")
        return jsonify({"error": "Invalid webhook signature."}), 403
    if not valid:
        return jsonify({"error": "Invalid webhook signature."}), 403
    provider_id = (request.form.get("MessageSid") or request.form.get("SmsSid") or "").strip()
    status = (request.form.get("MessageStatus") or request.form.get("SmsStatus") or "").strip().lower()
    allowed = {"accepted", "queued", "sending", "sent", "delivered", "read", "failed", "undelivered", "canceled"}
    if not provider_id or status not in allowed:
        return jsonify({"ok": True, "ignored": True}), 200
    log = NotificationLog.query.filter_by(provider_id=provider_id, channel="WhatsApp").order_by(NotificationLog.created_at.desc()).first()
    if log:
        log.status = status
        db.session.commit()
    return jsonify({"ok": True, "updated": bool(log)}), 200


@main_bp.get("/profile")
@login_required
def profile():
    user = current_user()
    email_count = EmailMessage.query.filter_by(user_id=user.id, is_trashed=False).count()
    alert_count = NotificationLog.query.filter_by(user_id=user.id).count()
    task_count = EmailTask.query.filter_by(user_id=user.id).count()
    return render_template(
        "profile.html",
        user=user,
        email_count=email_count,
        alert_count=alert_count,
        task_count=task_count,
    )


@main_bp.post("/profile/username")
@login_required
@limiter.limit("20 per hour")
def update_username():
    user = current_user()
    new_username = (request.form.get("username") or "").strip()

    if not new_username:
        flash("Username cannot be empty.", "warning")
        return redirect(url_for("main.profile"))

    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", new_username):
        flash(
            "Username must be between 3 and 80 characters and can only contain letters, numbers, dots, hyphens, and underscores.",
            "danger",
        )
        return redirect(url_for("main.profile"))

    if new_username.lower() != user.username.lower():
        existing = User.query.filter(
            db.func.lower(User.username) == new_username.lower()
        ).first()
        if existing and existing.id != user.id:
            flash(
                "That username is already taken. Please choose another one.",
                "warning",
            )
            return redirect(url_for("main.profile"))

    old_username = user.username
    user.username = new_username

    try:
        db.session.add(
            AuditLog(
                user_id=user.id,
                action="username_updated",
                details=f"Username updated from '{old_username}' to '{new_username}'",
            )
        )
        db.session.commit()
        flash("Username updated successfully!", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to update username for user %s", user.id)
        flash("Could not update username. Please try again.", "danger")

    return redirect(url_for("main.profile"))


@main_bp.post("/profile/photo")
@login_required
@limiter.limit("10 per hour")
def upload_profile_photo():
    user = current_user()
    uploaded = request.files.get("profile_image")

    if not uploaded or not uploaded.filename:
        flash("Please choose an image first.", "warning")
        return redirect(url_for("main.profile"))

    # Enforce the size limit even if the client bypasses browser validation.
    image_data = uploaded.stream.read(MAX_PROFILE_IMAGE_SIZE + 1)

    if len(image_data) > MAX_PROFILE_IMAGE_SIZE:
        flash("Image must be 10 MB or smaller.", "danger")
        return redirect(url_for("main.profile"))

    # Validate actual file signatures rather than trusting filenames or MIME types.
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    elif image_data.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif (
        len(image_data) >= 12
        and image_data[:4] == b"RIFF"
        and image_data[8:12] == b"WEBP"
    ):
        extension = ".webp"
    else:
        flash("Choose a valid JPG, PNG, or WebP image.", "danger")
        return redirect(url_for("main.profile"))

    upload_dir = (
        Path(current_app.static_folder)
        / "uploads"
        / "profile_images"
    )

    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        current_app.logger.exception("Could not create profile image directory")
        flash("Image storage is unavailable. Please try again.", "danger")
        return redirect(url_for("main.profile"))

    # Never use the user-supplied filename as a filesystem path.
    filename = f"user_{user.id}_{uuid.uuid4().hex}{extension}"
    destination = upload_dir / filename
    old_filename = user.profile_image

    try:
        destination.write_bytes(image_data)
        user.profile_image = filename

        db.session.add(
            AuditLog(
                user_id=user.id,
                action="profile_photo_updated",
                details="Profile photo uploaded",
            )
        )
        db.session.commit()

        # Remove the old photo only after the new one is saved successfully.
        if old_filename:
            safe_old_name = os.path.basename(old_filename)
            old_path = upload_dir / safe_old_name

            if old_path.is_file():
                try:
                    old_path.unlink()
                except OSError:
                    current_app.logger.warning(
                        "Could not remove previous profile photo for user %s",
                        user.id,
                    )

        flash("Profile photo updated successfully.", "success")

    except Exception:
        db.session.rollback()

        try:
            destination.unlink(missing_ok=True)
        except OSError:
            current_app.logger.warning(
                "Could not remove failed profile photo upload"
            )

        current_app.logger.exception(
            "Profile photo upload failed for user %s", user.id
        )
        flash("Could not save your profile photo. Please try again.", "danger")

    return redirect(url_for("main.profile"))


@main_bp.post("/profile/photo/remove")
@login_required
@limiter.limit("10 per hour")
def remove_profile_photo():
    """Remove the current user's profile photo and return to the profile page."""
    user = current_user()
    old_filename = user.profile_image

    if not old_filename:
        flash("There is no profile photo to remove.", "info")
        return redirect(url_for("main.profile"))

    upload_dir = Path(current_app.static_folder) / "uploads" / "profile_images"

    try:
        # Clear the database reference first so the account falls back to its initial.
        user.profile_image = None
        db.session.add(
            AuditLog(
                user_id=user.id,
                action="profile_photo_removed",
                details="Profile photo removed",
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Profile photo removal failed for user %s", user.id
        )
        flash("Could not remove your profile photo. Please try again.", "danger")
        return redirect(url_for("main.profile"))

    # Only remove a file inside the profile-image directory; never trust a stored path.
    safe_old_name = os.path.basename(old_filename)
    old_path = upload_dir / safe_old_name
    try:
        if old_path.is_file():
            old_path.unlink()
    except OSError:
        current_app.logger.warning(
            "Profile photo reference cleared, but old file could not be removed for user %s",
            user.id,
        )

    flash("Profile photo removed. Your default avatar is now displayed.", "success")
    return redirect(url_for("main.profile"))


@main_bp.get("/settings")
@login_required
def settings():
    return render_template("settings.html", user=current_user())


@main_bp.get("/assistant")
@login_required
def assistant_page():
    return render_template("assistant.html", user=current_user())


@main_bp.post("/chatbot")
@login_required
@limiter.limit("30 per minute")
def chat():
    """Grounded mailbox assistant; answers only from this user's stored records."""
    from sqlalchemy import or_
    from .models import EmailAttachment
    raw_value = (request.get_json(silent=True) or {}).get("question", "") if request.is_json else request.form.get("question", "")
    raw_question = str(raw_value or "").strip()[:500]
    question = raw_question.lower()
    user = current_user()
    if not question:
        return jsonify({"answer": "Type a question first.", "sources": []})

    stop_words = {"the", "and", "for", "with", "from", "that", "this", "show", "find", "search", "summarize", "summary", "today", "emails", "email", "inbox", "please", "what", "which", "have", "has", "are", "was", "were", "about", "list", "tell", "me", "can", "you", "my", "all", "get", "give", "any", "there", "does", "did", "when", "where", "who", "how", "much", "many", "attachment", "attachments", "document", "documents", "pdf", "docx"}
    terms = [re.sub(r"[^a-z0-9@._+-]", "", w) for w in question.split()]
    terms = [w for w in terms if len(w) >= 3 and w not in stop_words][:10]
    base = EmailMessage.query.filter_by(user_id=user.id, is_trashed=False, is_spam=False)

    if any(k in question for k in ("spam", "blocked sender", "blocklist")):
        spam_query = EmailMessage.query.filter_by(user_id=user.id, is_trashed=False, is_spam=True)
        count = spam_query.count()
        rows = spam_query.order_by(EmailMessage.received_at.desc()).limit(8).all()
        answer = f"NEXORA currently has {count} message(s) in Spam & Quarantine. The examples below are quarantined messages; review them before deleting anything because automatic scores are estimates."
        sources = [{"id": r.id, "subject": r.subject or "(No subject)", "sender": r.sender or "Unknown sender", "summary": r.spam_reason or r.summary or "Quarantined by NEXORA."} for r in rows]
        return jsonify({"answer": answer, "sources": sources, "suggestions": ["Show suspicious emails", "Find tasks and deadlines"]})
    if any(k in question for k in ("phishing", "suspicious link", "suspicious email")):
        rows = base.filter(EmailMessage.phishing_score >= 0.45).order_by(EmailMessage.received_at.desc()).limit(8).all()
        answer = f"I found {len(rows)} suspicious message(s) in the latest matching results. A warning is an indicator, not proof that a message is malicious. Do not open unexpected links or attachments."
        return jsonify({"answer": answer, "sources": [{"id": r.id, "subject": r.subject or "(No subject)", "sender": r.sender or "Unknown sender", "summary": r.phishing_reason or r.summary or "Review this message carefully."} for r in rows]})
    if any(k in question for k in ("task", "deadline", "todo", "action item", "due date")):
        tasks = EmailTask.query.filter_by(user_id=user.id).filter(EmailTask.status.in_(["open", "suggested"])).order_by(EmailTask.due_at.asc().nullslast(), EmailTask.created_at.desc()).limit(12).all()
        answer = "Your open or suggested tasks are listed below. Dates are extracted estimates; confirm them against the original email."
        sources = [{"id": t.email_id, "subject": t.title, "sender": "Task" + (" · due " + t.due_at.strftime("%d %b %Y %H:%M UTC") if t.due_at else " · no date detected"), "summary": t.source_text or ""} for t in tasks]
        return jsonify({"answer": answer if tasks else "I couldn't find any open tasks. Run Gmail sync, then review Tasks & Deadlines.", "sources": sources})
    if "attachment" in question or "document" in question or "pdf" in question or "docx" in question:
        query = EmailAttachment.query.filter_by(user_id=user.id)
        if terms:
            query = query.filter(or_(*[or_(EmailAttachment.filename.ilike(f"%{t}%"), EmailAttachment.extracted_text.ilike(f"%{t}%")) for t in terms[:6]]))
        attachments = query.order_by(EmailAttachment.created_at.desc()).limit(8).all()
        return jsonify({"answer": f"I found {len(attachments)} matching attachment record(s). Text extraction is supported for PDF, DOCX, TXT, CSV and HTML files within the configured size limit; unsupported files remain metadata-only.", "sources": [{"id": a.email_id, "subject": a.filename, "sender": a.mime_type or "Attachment", "summary": ((a.extracted_text or "No text extracted: " + a.extraction_status)[:260])} for a in attachments]})

    if terms:
        clauses = []
        for term in terms:
            pattern = f"%{term}%"
            clauses.append(or_(EmailMessage.sender.ilike(pattern), EmailMessage.subject.ilike(pattern), EmailMessage.body.ilike(pattern), EmailMessage.summary.ilike(pattern), EmailMessage.category.ilike(pattern)))
        query = base.filter(or_(*clauses))
    else:
        query = base
    matches = query.order_by(EmailMessage.received_at.desc()).limit(8).all()
    if any(k in question for k in ("summarize", "summary", "overview", "what happened", "today")) and not terms:
        matches = base.order_by(EmailMessage.received_at.desc()).limit(8).all()
    sources = [{"id": m.id, "subject": m.subject or "(No subject)", "sender": m.sender or "Unknown sender", "summary": (m.summary or m.body or "No message text")[:300]} for m in matches]
    attachment_matches = []
    if terms:
        attachment_query = EmailAttachment.query.filter_by(user_id=user.id).filter(or_(*[
            or_(EmailAttachment.filename.ilike(f"%{term}%"), EmailAttachment.extracted_text.ilike(f"%{term}%"))
            for term in terms[:6]
        ]))
        attachment_matches = attachment_query.order_by(EmailAttachment.created_at.desc()).limit(5).all()
        for attachment in attachment_matches:
            parent = db.session.get(EmailMessage, attachment.email_id)
            sources.append({
                "id": attachment.email_id,
                "subject": f"Attachment: {attachment.filename}",
                "sender": (parent.sender if parent else "Attachment") or "Attachment",
                "summary": (attachment.extracted_text or "No text extracted: " + attachment.extraction_status)[:300],
            })
    if matches or attachment_matches:
        answer = f"I found {len(matches)} relevant email(s) and {len(attachment_matches)} matching attachment(s) in your NEXORA mailbox. Results are grounded in stored records; open the source email to verify details."
    else:
        answer = "I couldn't find a matching email or extracted attachment text in the records currently stored in NEXORA. Try a sender address, subject keyword, or run Gmail monitoring to synchronize the mailbox."
    return jsonify({"answer": answer, "sources": sources, "suggestions": ["Summarize my inbox", "Find deadlines", "Search attachments", "Show suspicious emails"]})

@main_bp.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "NEXORA Notification Hub",
    })


@main_bp.post("/profile/role")
@login_required
def profile_role():
    # Deliberately does not allow a user to promote themselves.
    user = current_user()

    if user.role != "hr":
        flash("Role changes are restricted to an administrator.", "warning")
        return redirect(url_for("main.profile"))

    target = request.form.get("role")

    if target not in {"employee", "hr"}:
        flash("Invalid role.", "danger")
        return redirect(url_for("main.profile"))

    # Educational build: role display is supported, but no self-promotion workflow.
    flash("Role management is intentionally restricted in this build.", "info")
    return redirect(url_for("main.profile"))
