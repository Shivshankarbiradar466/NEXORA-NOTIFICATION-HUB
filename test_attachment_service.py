import base64
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "attachment_service.py"
spec = importlib.util.spec_from_file_location("attachment_service", MODULE_PATH)
attachment_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attachment_service)


def test_decode_gmail_data_padding():
    raw = b"hello attachment"
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert attachment_service.decode_gmail_data(encoded) == raw


def test_extract_plain_text():
    text, status = attachment_service.extract_text("notes.txt", "text/plain", b"Meeting at 10 AM\nSubmit the report")
    assert status == "extracted"
    assert "Submit the report" in text


def test_unsupported_attachment_is_not_executed():
    text, status = attachment_service.extract_text("script.exe", "application/octet-stream", b"not really an executable")
    assert text == ""
    assert status == "unsupported_type"


def test_oversized_attachment_is_skipped():
    text, status = attachment_service.extract_text("big.txt", "text/plain", b"x" * (5 * 1024 * 1024 + 1))
    assert text == ""
    assert status == "skipped_too_large"
