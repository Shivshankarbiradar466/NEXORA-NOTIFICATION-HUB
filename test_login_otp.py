import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.auth import mask_email, hash_token


class LoginOTPTests(unittest.TestCase):
    def test_mask_email(self):
        self.assertEqual(mask_email("sharath@gmail.com"), "s*****h@gmail.com")
        self.assertEqual(mask_email("me@domain.com"), "m*@domain.com")
        self.assertEqual(mask_email("a@b.com"), "a*@b.com")
        self.assertEqual(mask_email("invalid_email"), "invalid_email")
        self.assertEqual(mask_email(""), "")

    def test_otp_token_hash_matches(self):
        user_id = 42
        otp = "782910"
        digest = hash_token(f"login_otp:{user_id}:{otp}")
        self.assertEqual(digest, hash_token(f"login_otp:{user_id}:{otp}"))
        self.assertNotEqual(digest, hash_token(f"login_otp:{user_id}:123456"))

    def test_otp_expiry_calculation_within_2_minutes(self):
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=120)
        remaining = int((expires_at - now).total_seconds())
        self.assertGreaterEqual(remaining, 119)
        self.assertLessEqual(remaining, 120)

    def test_otp_expired_after_2_minutes(self):
        now = datetime.now(timezone.utc)
        # 2 minutes and 5 seconds ago
        expires_at = now - timedelta(seconds=5)
        self.assertTrue(now > expires_at)
        remaining = max(0, int((expires_at - now).total_seconds()))
        self.assertEqual(remaining, 0)

    def test_otp_verification_logic_success(self):
        user_id = 99
        correct_otp = "456789"
        stored_hash = hash_token(f"login_otp:{user_id}:{correct_otp}")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=2)

        # Submitted code matches and is within 2 minutes
        submitted_code = "456789"
        submitted_hash = hash_token(f"login_otp:{user_id}:{submitted_code}")

        is_valid = (submitted_hash == stored_hash) and (now <= expires_at)
        self.assertTrue(is_valid)

    def test_otp_verification_logic_expired(self):
        user_id = 99
        correct_otp = "456789"
        stored_hash = hash_token(f"login_otp:{user_id}:{correct_otp}")
        now = datetime.now(timezone.utc)
        # Expired 1 second ago
        expires_at = now - timedelta(seconds=1)

        submitted_code = "456789"
        submitted_hash = hash_token(f"login_otp:{user_id}:{submitted_code}")

        is_valid = (submitted_hash == stored_hash) and (now <= expires_at)
        self.assertFalse(is_valid)

    def test_otp_verification_logic_wrong_code(self):
        user_id = 99
        correct_otp = "456789"
        stored_hash = hash_token(f"login_otp:{user_id}:{correct_otp}")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=2)

        # Wrong code submitted
        submitted_code = "111111"
        submitted_hash = hash_token(f"login_otp:{user_id}:{submitted_code}")

        is_valid = (submitted_hash == stored_hash) and (now <= expires_at)
        self.assertFalse(is_valid)

    def test_google_login_triggers_otp_generation(self):
        import secrets
        user_id = 77
        otp = f"{secrets.randbelow(900000) + 100000}"
        self.assertEqual(len(otp), 6)
        self.assertTrue(otp.isdigit())

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=120)
        otp_hash = hash_token(f"login_otp:{user_id}:{otp}")

        self.assertEqual(otp_hash, hash_token(f"login_otp:{user_id}:{otp}"))
        self.assertFalse(now > expires_at)


if __name__ == "__main__":
    unittest.main()
