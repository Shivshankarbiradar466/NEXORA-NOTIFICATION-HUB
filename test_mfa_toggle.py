import os
import unittest

os.environ["FLASK_TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FLASK_SECRET_KEY"] = "a" * 32
os.environ["RATELIMIT_STORAGE_URI"] = "memory://"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from app import create_app, db, limiter
from app.models import User, AuditLog


class MFAToggleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.app.config["WTF_CSRF_ENABLED"] = False
        cls.app.config["RATELIMIT_ENABLED"] = False
        limiter.enabled = False
        with cls.app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.drop_all()

    def setUp(self):
        self.client = self.app.test_client()

    def test_dashboard_toggle_mfa_on_and_off(self):
        with self.app.app_context():
            user = User(
                username="mfa_toggle_user",
                email="mfa_toggle@example.com",
                role="employee",
                password_hash="dummy_hash",
                mfa_enabled=False,
            )
            db.session.add(user)
            db.session.commit()
            uid = user.id
            s_ver = user.session_version

        # Authenticate session
        with self.client.session_transaction() as sess:
            sess["user_id"] = uid
            sess["session_version"] = s_ver
            sess["_fresh"] = True

        # 1. Turn ON 2-Step Verification from dashboard
        res_on = self.client.post("/mfa/toggle", data={"action": "start"})
        self.assertEqual(res_on.status_code, 302)

        with self.app.app_context():
            u = db.session.get(User, uid)
            self.assertTrue(u.mfa_enabled)
            self.assertIsNotNone(u.mfa_secret)
            audit = AuditLog.query.filter_by(user_id=uid, action="mfa_enabled").first()
            self.assertIsNotNone(audit)

        # 2. Turn OFF 2-Step Verification from dashboard
        res_off = self.client.post("/mfa/toggle", data={"action": "stop"})
        self.assertEqual(res_off.status_code, 302)

        with self.app.app_context():
            u = db.session.get(User, uid)
            self.assertFalse(u.mfa_enabled)
            audit_off = AuditLog.query.filter_by(user_id=uid, action="mfa_disabled").first()
            self.assertIsNotNone(audit_off)

    def test_toggle_with_google_redirect(self):
        with self.app.app_context():
            user = User(
                username="mfa_google_user",
                email="mfa_google@example.com",
                role="employee",
                password_hash="dummy_hash",
                mfa_enabled=False,
            )
            db.session.add(user)
            db.session.commit()
            uid = user.id
            s_ver = user.session_version

        with self.client.session_transaction() as sess:
            sess["user_id"] = uid
            sess["session_version"] = s_ver
            sess["_fresh"] = True

        # Toggle ON and redirect directly to Google Account 2-Step Verification
        res = self.client.post("/mfa/toggle", data={"action": "start", "redirect_to_google": "1"})
        self.assertEqual(res.status_code, 302)
        self.assertIn("myaccount.google.com/signinoptions/two-step-verification", res.headers["Location"])

        # Also test the GET shortcut /mfa/google
        res_shortcut = self.client.get("/mfa/google")
        self.assertEqual(res_shortcut.status_code, 302)
        self.assertIn("myaccount.google.com/signinoptions/two-step-verification", res_shortcut.headers["Location"])


if __name__ == "__main__":
    unittest.main()
