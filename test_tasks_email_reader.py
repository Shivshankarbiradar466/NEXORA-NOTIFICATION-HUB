import os
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FLASK_SECRET_KEY"] = "a" * 32
os.environ["RATELIMIT_STORAGE_URI"] = "memory://"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from app import create_app, db, limiter
from app.models import User, EmailMessage, EmailTask

class TasksEmailReaderTestCase(unittest.TestCase):
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

    def test_api_emails_endpoint_and_tasks_page(self):
        with self.app.app_context():
            user = User(
                username="readertest",
                email="test_reader@example.com",
                role="employee",
                password_hash="dummy_hash",
            )
            db.session.add(user)
            db.session.commit()

            test_email = EmailMessage(
                user_id=user.id,
                message_id="msg_test_task_reader_1",
                sender="Alice <alice@example.com>",
                recipient="test_reader@example.com",
                subject="Project Deadline Review",
                body="<p>Please review the project roadmap before tomorrow.</p>",
                category="Work",
                priority="High",
                summary="Review project roadmap before tomorrow.",
                is_read=False,
            )
            db.session.add(test_email)
            db.session.commit()

            task = EmailTask(
                user_id=user.id,
                email_id=test_email.id,
                title="Review project roadmap",
                source_text="Please review the project roadmap before tomorrow.",
                status="open",
            )
            db.session.add(task)
            db.session.commit()

            email_id = test_email.id
            user_id = user.id
            session_version = user.session_version

        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["session_version"] = session_version

        # 1. Test GET /api/emails/<email_id>
        res = self.client.get(f"/api/emails/{email_id}")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["id"], email_id)
        self.assertEqual(data["subject"], "Project Deadline Review")
        self.assertEqual(data["sender"], "Alice <alice@example.com>")
        self.assertTrue(data["is_html"])
        self.assertIn("roadmap", data["body_html"])
        self.assertIn("roadmap", data["body_plain"])
        self.assertIn("Review project roadmap", data["summary"])

        # Verify email is now marked as read
        with self.app.app_context():
            refreshed = db.session.get(EmailMessage, email_id)
            self.assertTrue(refreshed.is_read)

        # 2. Test GET /tasks renders reader modal and buttons
        tasks_res = self.client.get("/tasks")
        self.assertEqual(tasks_res.status_code, 200)
        html = tasks_res.get_data(as_text=True)
        self.assertIn("emailReaderModal", html)
        self.assertIn("js-open-email-modal", html)
        self.assertIn("Open & Read Mail", html)

if __name__ == "__main__":
    unittest.main()

