"""Task deadline reminder service for NEXORA.

Sweeps for open and suggested tasks approaching their deadlines (or recently overdue)
and dispatches proactive WhatsApp reminders.
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def sweep_and_send_task_reminders(app=None):
    """Scan and send WhatsApp reminders for tasks with approaching or past deadlines.

    Reminds for tasks where:
    - Status is 'open' or 'suggested'
    - due_at is not None
    - reminder_sent_at is None
    - Due within the next 24 hours or overdue in past 48 hours
    """
    from .. import db
    from ..models import EmailTask, User, NotificationLog
    from .whatsapp import send_whatsapp

    now = datetime.now(timezone.utc)
    upper = now + timedelta(hours=24)
    lower = now - timedelta(hours=48)
    sent_count = 0

    try:
        tasks = (
            EmailTask.query.filter(
                EmailTask.status.in_(["open", "suggested"]),
                EmailTask.due_at.isnot(None),
                EmailTask.reminder_sent_at.is_(None),
            )
            .limit(500)
            .all()
        )

        for task in tasks:
            due = task.due_at
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)

            # Only remind if due within 24 hours or overdue in past 48 hours
            if due < lower or due > upper:
                continue

            user = db.session.get(User, task.user_id)
            if not user or not user.whatsapp_number:
                continue

            due_text = due.strftime("%d %b %Y, %I:%M %p UTC")
            if due < now:
                status_header = "⚠️ TASK OVERDUE"
                status_desc = "This task has passed its deadline and requires immediate attention."
            else:
                status_header = "⏰ TASK DEADLINE REMINDER"
                status_desc = "This task is approaching its deadline (due within 24 hours)."

            email_details = ""
            if task.email:
                subj = (task.email.subject or "(No Subject)")[:120]
                sender = (task.email.sender or "Unknown")[:80]
                email_details = f"\nSource Email: \"{subj}\"\nFrom: {sender}"

            message = (
                f"{status_header}\n\n"
                f"Task: {task.title[:350]}\n"
                f"Deadline: {due_text}\n"
                f"Status: {status_desc}"
                f"{email_details}\n\n"
                f"Please take action to complete this task on time."
            )

            result = send_whatsapp(user.whatsapp_number, message)
            log = NotificationLog(
                user_id=user.id,
                channel="WhatsApp",
                destination=user.whatsapp_number,
                message=message,
                status="sent" if result.get("ok") else "failed",
                provider_id=result.get("sid"),
            )
            db.session.add(log)
            if result.get("ok"):
                task.reminder_sent_at = now
                sent_count += 1
            db.session.commit()

        if sent_count > 0:
            logger.info("Task deadline sweep: sent %d reminder(s).", sent_count)
        return sent_count

    except Exception as exc:
        db.session.rollback()
        logger.exception("Task deadline reminder sweep failed: %s", exc)
        return 0
