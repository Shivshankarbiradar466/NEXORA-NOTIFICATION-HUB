import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "email_intelligence.py"
spec = importlib.util.spec_from_file_location("email_intelligence", MODULE_PATH)
intelligence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intelligence)

class EmailIntelligenceTests(unittest.TestCase):
    def test_blocked_sender_scores_as_spam(self):
        result = intelligence.analyse_email("Offer <sales@example.com>", "Hello", "Regular message", blocked=True)
        self.assertEqual(result["spam_score"], 1.0)
        self.assertTrue(result["is_spam"])

    def test_phishing_language_is_flagged(self):
        result = intelligence.analyse_email("Support <help@example.com>", "Security alert", "Verify your password immediately or your account will be suspended")
        self.assertGreaterEqual(result["phishing_score"], 0.45)
        self.assertTrue(result["phishing_reason"])

    def test_trusted_sender_reduces_spam_score(self):
        result = intelligence.analyse_email("Friend <friend@example.com>", "Claim your prize today", "You have won, click here to claim", trusted=True)
        self.assertLessEqual(result["spam_score"], 0.15)

    def test_extract_action_item(self):
        tasks = intelligence.extract_action_items("Assignment", "Please submit the assignment by Friday.")
        self.assertTrue(tasks)
        self.assertIn("submit", tasks[0].lower())

    def test_parse_explicit_due_date(self):
        due = intelligence.extract_due_date("Please submit this by 31/12/2030")
        self.assertIsNotNone(due)
        self.assertEqual(due.date(), datetime(2030, 12, 31).date())

if __name__ == "__main__":
    unittest.main()
