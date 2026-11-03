from .celery_app import celery
from . import db, create_app
from .models import User, NotificationLog, EmailTask
from .services.email_service import monitor_inbox
from .services.whatsapp import send_whatsapp

@celery.task(bind=True, name="app.tasks.monitor_user")
def monitor_user(self, user_id):
    app = create_app()
    with app.app_context():
        try:
            user = db.session.get(User, user_id)
            if not user:
                return {"ok": False, "error": "User not found"}
            if not user.monitoring_enabled:
                return {"ok": True, "count": 0, "skipped": "monitoring disabled"}
            count = monitor_inbox(user)
            return {"ok": True, "count": count}
        except Exception as exc:
            db.session.rollback()
            app.logger.exception("Gmail monitoring task failed for user %s", user_id)
            return {"ok": False, "error": str(exc)}
        finally:
            db.session.remove()

@celery.task(bind=True, name="app.tasks.whatsapp_task")
def whatsapp_task(self, user_id, message):
    app = create_app()
    with app.app_context():
        try:
            user = db.session.get(User, user_id)
            if not user or not user.whatsapp_number:
                return {"ok": False, "error": "WhatsApp number not configured"}
            result = send_whatsapp(user.whatsapp_number, message)
            log = NotificationLog(
                user_id=user.id,
                channel="WhatsApp",
                destination=user.whatsapp_number,
                message=message,
                status=result.get("status") if result.get("ok") else "failed",
                provider_id=result.get("sid"),
            )
            db.session.add(log)
            db.session.commit()
            return result
        except Exception as exc:
            db.session.rollback()
            app.logger.exception("WhatsApp task failed")
            return {"ok": False, "error": str(exc)}
        finally:
            db.session.remove()

@celery.task(bind=True, name="app.tasks.monitor_enabled_users")
def monitor_enabled_users(self):
    app = create_app()
    with app.app_context():
        try:
            ids = [u.id for u in User.query.filter_by(monitoring_enabled=True).all()]
            task_ids = []
            for user_id in ids:
                task_ids.append(monitor_user.delay(user_id).id)
            return {"queued_users": len(ids), "task_ids": task_ids}
        finally:
            db.session.remove()


@celery.task(bind=True, name="app.tasks.send_due_task_reminders")
def send_due_task_reminders(self):
    """Send one WhatsApp reminder for tasks approaching or past due."""
    app = create_app()
    with app.app_context():
        try:
            from .services.task_reminder_service import sweep_and_send_task_reminders
            sent_count = sweep_and_send_task_reminders(app)
            return {"ok": True, "reminders_sent": sent_count}
        except Exception as exc:
            app.logger.exception("Task reminder sweep failed")
            return {"ok": False, "error": str(exc)}
        finally:
            db.session.remove()


@celery.task(bind=True, name="app.tasks.sweep_auto_ai_replies")
def sweep_auto_ai_replies(self):
    """Scan and send automated AI replies for emails unanswered after 6 hours."""
    app = create_app()
    with app.app_context():
        try:
            from .services.auto_reply_service import process_auto_ai_replies
            sent_count = process_auto_ai_replies()
            return {"ok": True, "replies_sent": sent_count}
        except Exception as exc:
            app.logger.exception("Auto AI reply sweep task failed")
            return {"ok": False, "error": str(exc)}
        finally:
            db.session.remove()
