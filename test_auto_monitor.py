import os
import unittest
import time

os.environ["FLASK_TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FLASK_SECRET_KEY"] = "a" * 32
os.environ["RATELIMIT_STORAGE_URI"] = "memory://"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from app import create_app, db, limiter
from app.models import User, EmailMessage
from app.services.continuous_monitor import (
    can_scan_user,
    record_user_scan,
    acquire_user_scan,
    release_user_scan,
)


class AutoMonitorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.app.config["WTF_CSRF_ENABLED"] = False
        cls.app.config["RATELIMIT_ENABLED"] = False
        cls.app.config["RATELIMIT_STORAGE_URI"] = "memory://"
        limiter.enabled = False
        with cls.app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.drop_all()

    def setUp(self):
        self.client = self.app.test_client()

    def test_continuous_monitor_debounce_and_locks(self):
        user_id = 9999
        # Initially, can scan
        self.assertTrue(can_scan_user(user_id, min_interval=1.0))

        # Acquire lock
        self.assertTrue(acquire_user_scan(user_id, min_interval=1.0))
        # Cannot acquire again while active
        self.assertFalse(acquire_user_scan(user_id, min_interval=1.0))
        self.assertFalse(can_scan_user(user_id, min_interval=1.0))

        # Release lock
        release_user_scan(user_id)
        # Immediately after release, within min_interval, cannot scan
        self.assertFalse(can_scan_user(user_id, min_interval=5.0))

        # But can scan with 0 interval
        self.assertTrue(can_scan_user(user_id, min_interval=0.0))

    def test_live_poll_endpoint(self):
        with self.app.app_context():
            user = User(
                username="monitortestuser",
                email="monitor_test@example.com",
                role="employee",
                password_hash="dummy_hash",
                monitoring_enabled=True,
            )
            db.session.add(user)
            db.session.commit()

            msg1 = EmailMessage(
                user_id=user.id,
                message_id="msg_live_poll_1",
                sender="CEO <ceo@company.com>",
                recipient=user.email,
                subject="Urgent Board Review",
                body="Please review the board materials ASAP.",
                summary="Board review requested urgently.",
                category="Work",
                priority="Urgent",
                is_read=False,
            )
            db.session.add(msg1)
            db.session.commit()

            user_id = user.id
            session_version = user.session_version
            msg1_id = msg1.id

        # Authenticate via session
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["session_version"] = session_version
            sess["_fresh"] = True

        # Call live-poll without since_id
        res = self.client.get("/api/monitor/live-poll?scan=0")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["monitoring_enabled"])
        self.assertEqual(data["total_emails"], 1)
        self.assertEqual(data["unread_emails"], 1)
        self.assertEqual(data["urgent_count"], 1)
        self.assertEqual(data["latest_id"], msg1_id)
        self.assertEqual(len(data["new_incoming"]), 0)

        # Add another email
        with self.app.app_context():
            msg2 = EmailMessage(
                user_id=user_id,
                message_id="msg_live_poll_2",
                sender="Colleague <colleague@company.com>",
                recipient="monitor_test@example.com",
                subject="Lunch plans",
                body="Are you free for lunch?",
                summary="Lunch invite.",
                category="General",
                priority="Normal",
                is_read=False,
            )
            db.session.add(msg2)
            db.session.commit()
            msg2_id = msg2.id

        # Call live-poll with since_id = msg1_id
        res2 = self.client.get(f"/api/monitor/live-poll?since_id={msg1_id}&scan=0")
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()
        self.assertEqual(data2["total_emails"], 2)
        self.assertEqual(data2["latest_id"], msg2_id)
        self.assertEqual(len(data2["new_incoming"]), 1)
        self.assertEqual(data2["new_incoming"][0]["id"], msg2_id)
        self.assertEqual(data2["new_incoming"][0]["subject"], "Lunch plans")
        self.assertEqual(data2["new_incoming"][0]["is_read"], False)

    def test_whatsapp_queue_and_dispatch(self):
        from unittest.mock import patch
        from app.services.email_service import _queue_whatsapp

        with self.app.app_context():
            user = User(
                username="wa_test_user",
                email="wa_test@example.com",
                whatsapp_number="+919876543210",
                monitoring_enabled=True,
            )
            db.session.add(user)
            db.session.commit()

            with patch("app.services.whatsapp.send_whatsapp") as mock_send:
                mock_send.return_value = {"ok": True, "sid": "SM12345", "status": "sent"}
                from app.models import EmailTask
                from datetime import datetime, timezone, timedelta
                dummy_due = datetime.now(timezone.utc) + timedelta(days=1)
                task1 = EmailTask(
                    user_id=user.id,
                    title="Submit the quarterly report",
                    due_at=dummy_due,
                    status="open",
                )
                result = _queue_whatsapp(
                    user=user,
                    sender="Team <team@example.com>",
                    subject="Sprint Retrospective",
                    category="Work",
                    priority="Normal",
                    confidence=0.88,
                    confidentiality="Internal",
                    summary="Meeting today to review sprint accomplishments.",
                    body="Hi team, call me at +919876543210 if any questions.",
                    tasks=[task1],
                )
                self.assertTrue(result.get("queued"))
                self.assertTrue(result.get("direct_send"))
                mock_send.assert_called_once()
                sent_msg = mock_send.call_args[0][1]
                self.assertIn("TASKS & DEADLINES TO DO:", sent_msg)
                self.assertIn("Submit the quarterly report", sent_msg)
                self.assertIn("Deadline:", sent_msg)

    def test_sweep_and_send_task_reminders(self):
        from unittest.mock import patch
        from datetime import datetime, timezone, timedelta
        from app.models import EmailTask
        from app.services.task_reminder_service import sweep_and_send_task_reminders

        with self.app.app_context():
            user = User(
                username="reminder_test_user",
                email="remind_test@example.com",
                whatsapp_number="+919876543210",
                monitoring_enabled=True,
            )
            db.session.add(user)
            db.session.commit()

            # Task 1: Due in 6 hours (approaching deadline) -> should be reminded
            now = datetime.now(timezone.utc)
            t1 = EmailTask(
                user_id=user.id,
                title="Prepare investor presentation",
                due_at=now + timedelta(hours=6),
                status="open",
            )
            # Task 2: Already reminded -> should NOT be reminded
            t2 = EmailTask(
                user_id=user.id,
                title="Review terms of service",
                due_at=now + timedelta(hours=8),
                reminder_sent_at=now - timedelta(hours=1),
                status="open",
            )
            # Task 3: Completed -> should NOT be reminded
            t3 = EmailTask(
                user_id=user.id,
                title="Fix payment gateway bug",
                due_at=now + timedelta(hours=5),
                status="done",
            )
            # Task 4: Due in 10 days -> too far in future, should NOT be reminded yet
            t4 = EmailTask(
                user_id=user.id,
                title="Renew SSL certificates",
                due_at=now + timedelta(days=10),
                status="open",
            )
            db.session.add_all([t1, t2, t3, t4])
            db.session.commit()

            with patch("app.services.whatsapp.send_whatsapp") as mock_send:
                mock_send.return_value = {"ok": True, "sid": "SM99999", "status": "sent"}
                sent_count = sweep_and_send_task_reminders(self.app)
                self.assertEqual(sent_count, 1)
                mock_send.assert_called_once()
                msg = mock_send.call_args[0][1]
                self.assertIn("TASK DEADLINE REMINDER", msg)
                self.assertIn("Prepare investor presentation", msg)

            # Check that t1 now has reminder_sent_at set
            t1_refreshed = db.session.get(EmailTask, t1.id)
            self.assertIsNotNone(t1_refreshed.reminder_sent_at)

            # Running again should send 0 because t1 is now marked reminded
            with patch("app.services.whatsapp.send_whatsapp") as mock_send_again:
                sent_count_again = sweep_and_send_task_reminders(self.app)
                self.assertEqual(sent_count_again, 0)
                mock_send_again.assert_not_called()

    def test_privacy_policy_page(self):
        res = self.client.get("/privacy")
        self.assertEqual(res.status_code, 200)
        content = res.get_data(as_text=True)
        self.assertIn("Privacy Policy", content)
        self.assertIn("Google API User Data Policy Compliance", content)
        self.assertIn("Limited Use requirements", content)


if __name__ == "__main__":
    unittest.main()
