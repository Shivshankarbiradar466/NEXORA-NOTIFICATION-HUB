"""Safe, bounded text extraction for common email attachment formats.

Only extracted text and metadata are stored; attachment binaries remain in Gmail.
Unsupported or oversized files are recorded as such rather than executed.
"""
import base64
import csv
import io
import re
from html import unescape

MAX_EXTRACT_BYTES = 5 * 1024 * 1024
MAX_EXTRACT_CHARS = 120_000
SUPPORTED_TEXT_TYPES = {
    "text/plain", "text/csv", "text/html", "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def decode_gmail_data(data):
    if not data:
        return b""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _clean(text):
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_EXTRACT_CHARS]


def extract_text(filename, mime_type, raw_bytes):
    """Return (text, status). Never execute attachment content."""
    mime = (mime_type or "application/octet-stream").split(";", 1)[0].lower()
    name = (filename or "").lower()
    if len(raw_bytes or b"") > MAX_EXTRACT_BYTES:
        return "", "skipped_too_large"
    if not raw_bytes:
        return "", "empty"
    try:
        if mime == "application/pdf" or name.endswith(".pdf"):
            try:
                from pypdf import PdfReader
            except ImportError:
                return "", "dependency_missing_pypdf"
            reader = PdfReader(io.BytesIO(raw_bytes), strict=False)
            chunks = []
            for page in reader.pages[:100]:
                chunks.append(page.extract_text() or "")
            text = "\n".join(chunks)
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or name.endswith(".docx"):
            try:
                from docx import Document
            except ImportError:
                return "", "dependency_missing_python_docx"
            document = Document(io.BytesIO(raw_bytes))
            text = "\n".join(p.text for p in document.paragraphs)
            for table in document.tables[:20]:
                for row in table.rows[:100]:
                    text += "\n" + " | ".join(cell.text for cell in row.cells)
        elif mime in {"text/plain", "text/csv"} or name.endswith((".txt", ".csv", ".md", ".log")):
            text = raw_bytes.decode("utf-8-sig", errors="replace")
            if mime == "text/csv" or name.endswith(".csv"):
                rows = list(csv.reader(io.StringIO(text)))[:500]
                text = "\n".join(" | ".join(row) for row in rows)
        elif mime == "text/html" or name.endswith((".html", ".htm")):
            html = raw_bytes.decode("utf-8", errors="replace")
            html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
            text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
        else:
            return "", "unsupported_type"
        cleaned = _clean(text)
        return cleaned, ("extracted" if cleaned else "no_text_found")
    except Exception:
        return "", "extraction_failed"
