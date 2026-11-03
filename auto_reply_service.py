"""Automated AI Email Reply Engine.

Periodically evaluates received emails. If the user has Auto AI Reply enabled and
an incoming non-spam email has remained unreplied for at least 6 hours, it automatically
generates a context-aware AI reply, dispatches it via Gmail API, and logs the action.
"""

from datetime import datetime, timezone, timedelta
import logging
from .. import db
from ..models import User, EmailMessage, AuditLog, NotificationLog
from .reply_generator import generate_ai_reply
from .email_service import send_gmail_reply

logger = logging.getLogger(__name__)

# Automated notification / noreply sender indicators to exclude from auto-replies
EXCLUDED_SENDER_PATTERNS = (
    "no-reply", "noreply", "donotreply", "mailer-daemon", "postmaster",
    "notifications@", "notification@", "automated@", "auto-confirm",
    "bounce", "billing-alert", "newsletter", "system@", "alert@"
)


def _is_automated_sender(sender_str):
    if not sender_str:
        return True
    s = sender_str.lower()
    return any(pat in s for pat in EXCLUDED_SENDER_PATTERNS)


def process_auto_ai_replies(user_id=None):
    """Scan and dispatch automated AI replies for emails older than 6 hours."""
    now = datetime.now(timezone.utc)
    users_query = User.query.filter_by(auto_reply_enabled=True)
    if user_id:
        users_query = users_query.filter_by(id=user_id)

    users = users_query.all()
    total_replies_sent = 0

    for user in users:
        try:
            delay_hours = getattr(user, "auto_reply_delay_hours", 6) or 6
            cutoff = now - timedelta(hours=delay_hours)

            # Query candidate emails: received before cutoff, not spam, not trashed, not replied
            pending_emails = (
                EmailMessage.query.filter(
                    EmailMessage.user_id == user.id,
                    EmailMessage.is_trashed.is_(False),
                    EmailMessage.is_spam.is_(False),
                    EmailMessage.phishing_score < 0.45,
                    EmailMessage.status != "replied",
                    EmailMessage.auto_reply_sent_at.is_(None),
                    EmailMessage.received_at <= cutoff,
                )
                .order_by(EmailMessage.received_at.asc())
                .limit(20)
                .all()
            )

            for em in pending_emails:
                # Do not reply to automated/robot senders or empty subjects
                if _is_automated_sender(em.sender):
                    continue

                # Ensure contextual AI reply is ready
                if not em.ai_reply or len(em.ai_reply.strip()) < 10:
                    em.ai_reply = generate_ai_reply(em, user)
                    db.session.commit()

                reply_text = em.ai_reply.strip()
                if not reply_text:
                    continue

                try:
                    # Dispatch reply via user's connected Gmail OAuth token
                    send_gmail_reply(user, em.message_id, reply_text)

                    em.status = "replied"
                    em.auto_reply_sent_at = now

                    db.session.add(
                        AuditLog(
                            user_id=user.id,
                            action="auto_ai_reply_sent",
                            details=f"email_id={em.id}, to={em.sender}, delay_hours={delay_hours}",
                        )
                    )
                    db.session.commit()
                    total_replies_sent += 1
                    logger.info("Auto AI reply successfully sent for email_id=%s, user_id=%s", em.id, user.id)

                    # Trigger WhatsApp notification if user has configured WhatsApp alerts
                    if user.whatsapp_number:
                        try:
                            from ..tasks import whatsapp_task
                            whatsapp_task.delay(
                                user.id,
                                f"NEXORA • Auto AI Reply Dispatched\n\n"
                                f"To: {em.sender}\n"
                                f"Subject: Re: {em.subject or 'Your Inquiry'}\n"
                                f"Status: Contextual AI reply delivered after {delay_hours}h of no response."
                            )
                        except Exception:
                            pass

                except Exception as send_err:
                    db.session.rollback()
                    logger.warning("Failed to dispatch auto AI reply for email_id=%s: %s", em.id, send_err)

        except Exception as user_err:
            db.session.rollback()
            logger.exception("Error processing auto AI replies for user_id=%s: %s", user.id, user_err)

    return total_replies_sent
