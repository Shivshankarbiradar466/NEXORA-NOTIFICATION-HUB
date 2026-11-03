"""Local, explainable email spam/phishing and action-item analysis.

This module intentionally works without an external LLM or paid API. Scores are
heuristics, not guarantees; UI copy must never present them as definitive proof.
"""
import re
from functools import lru_cache
from email.utils import parseaddr
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

SPAM_PHRASES = (
    "claim your prize", "you have won", "winner selected", "act now",
    "limited time offer", "earn money fast", "work from home and earn",
    "verify your account immediately", "click here to claim", "risk free",
    "guaranteed returns", "urgent payment required", "gift card", "crypto investment",
    "congratulations you won", "free iphone", "lottery winner", "unsubscribe now",
)
PHISHING_PHRASES = (
    "verify your password", "account will be suspended", "confirm your credentials",
    "unusual sign-in", "your account is locked", "update your payment information",
    "security alert click", "send your otp", "share your password",
)
URL_RE = re.compile(r"https?://[^\s<>\]\[\)\}\"']+", re.I)


def _sender_parts(sender):
    _, address = parseaddr(sender or "")
    address = address.lower().strip()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    return address, domain


@lru_cache(maxsize=1)
def _load_spam_model(model_path):
    try:
        import joblib
        return joblib.load(model_path) if model_path else None
    except Exception:
        return None


def analyse_email(sender, subject, body, blocked=False, trusted=False):
    text = f"{subject or ''}\n{body or ''}"
    low = text.lower()
    reasons = []
    score = 0.0
    if blocked:
        score = 1.0
        reasons.append("Sender matches your personal blocklist")
    else:
        matches = [p for p in SPAM_PHRASES if p in low]
        if matches:
            score += min(0.60, 0.18 * len(matches))
            reasons.append("Promotional or unsolicited-message patterns: " + ", ".join(matches[:3]))
        if len(re.findall(r"!{2,}|\${2,}|\bFREE\b", text)) >= 2:
            score += 0.12
            reasons.append("Repeated promotional symbols or wording")
        if re.search(r"\b(?:cash prize|wire transfer|bank details|send money now)\b", low):
            score += 0.20
            reasons.append("Unexpected money-transfer language")
        # Optional scikit-learn model trained from the included starter CSV.
        # Heuristics remain visible and keep the system functional if model loading fails.
        try:
            from flask import current_app
            model_path = current_app.config.get("SPAM_MODEL_PATH", "")
            model = _load_spam_model(model_path)
            if model is not None and hasattr(model, "predict_proba"):
                classes = list(model.classes_)
                if "spam" in classes:
                    probabilities = model.predict_proba([text])[0]
                    ml_score = float(probabilities[classes.index("spam")])
                    score = max(score, 0.90 * ml_score)
                    reasons.append(f"Starter ML model spam probability: {ml_score:.0%}")
        except Exception:
            pass
        if trusted:
            score = min(score, 0.15)
            reasons = ["Trusted sender: automatic spam score reduced"] if score else []
    spam_score = max(0.0, min(1.0, score))

    phishing_score = 0.0
    phishing_reasons = []
    phrase_hits = [p for p in PHISHING_PHRASES if p in low]
    if phrase_hits:
        phishing_score += min(0.60, 0.24 * len(phrase_hits))
        phishing_reasons.append("Credential or account-pressure language detected")
    sender, sender_domain = _sender_parts(sender)
    urls = URL_RE.findall(text)
    for raw in urls[:30]:
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").lower().strip(".")
            if parsed.scheme != "https":
                phishing_score += 0.08
                phishing_reasons.append("Link does not use HTTPS")
            if re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", host):
                phishing_score += 0.30
                phishing_reasons.append("Link uses an IP address instead of a normal domain")
            if host.count("-") >= 3 or len(host) > 45:
                phishing_score += 0.12
                phishing_reasons.append("Unusual link hostname")
            if sender_domain and host and sender_domain not in (host, *host.split('.')) and any(x in host for x in ("login", "verify", "secure", "account")):
                phishing_score += 0.12
                phishing_reasons.append("Link hostname may not match the sender's domain")
        except Exception:
            continue
    phishing_score = max(0.0, min(1.0, phishing_score))
    # Conservative automatic quarantine: user override always wins in caller.
    auto_spam = spam_score >= 0.78
    suspicious = phishing_score >= 0.45
    return {
        "spam_score": round(spam_score, 3),
        "spam_reason": "; ".join(dict.fromkeys(reasons)) or "No strong spam indicators found",
        "is_spam": auto_spam,
        "phishing_score": round(phishing_score, 3),
        "phishing_reason": "; ".join(dict.fromkeys(phishing_reasons)) or "No strong phishing indicators found",
        "is_suspicious": suspicious,
    }


def extract_action_items(subject, body):
    """Return conservative task suggestions; does not create calendar events."""
    text = f"{subject or ''}\n{body or ''}"
    patterns = (
        r"[^\n.!?]{0,180}\b(?:please submit|submit|complete|finish|send|review|prepare|attend|pay|register|schedule)\b[^\n.!?]{0,180}",
        r"[^\n.!?]{0,180}\b(?:due|deadline|by tomorrow|by monday|by tuesday|by wednesday|by thursday|by friday)\b[^\n.!?]{0,180}",
    )
    found = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            candidate = " ".join(m.group(0).split()).strip(" -•\t")
            if len(candidate) >= 12 and candidate.lower() not in {x.lower() for x in found}:
                found.append(candidate[:500])
            if len(found) >= 5:
                return found
    return found



def extract_due_date(text):
    """Parse explicit common dates and simple relative deadlines; return UTC datetime or None."""
    from datetime import date, datetime, timedelta, timezone
    import calendar
    value = (text or "").lower()
    now = datetime.now(timezone.utc)

    if re.search(r"\b(?:today|eod|end of day)\b", value):
        return now.replace(hour=23, minute=59, second=0, microsecond=0)

    if re.search(r"\btomorrow\b", value):
        return (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)

    in_days_match = re.search(r"\bin\s+(\d+)\s+days?\b", value)
    if in_days_match:
        try:
            delta_days = int(in_days_match.group(1))
            return (now + timedelta(days=delta_days)).replace(hour=23, minute=59, second=0, microsecond=0)
        except ValueError:
            pass

    weekday_match = re.search(r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", value)
    if weekday_match:
        days = {name.lower(): i for i, name in enumerate(calendar.day_name)}
        target = days[weekday_match.group(1).title()]
        delta = (target - now.weekday()) % 7 or 7
        return (now + timedelta(days=delta)).replace(hour=23, minute=59, second=0, microsecond=0)

    month_names_regex = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    patterns = [
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", "%Y-%m-%d"),
        (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", "%d/%m/%Y"),
        (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names_regex})\b", "day_first"),
        (rf"\b({month_names_regex})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", "month_first"),
    ]
    month_map = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
    for pattern, fmt in patterns:
        match = re.search(pattern, value, re.I)
        if not match:
            continue
        try:
            if fmt == "%Y-%m-%d":
                parsed = datetime.strptime(match.group(0), fmt)
            elif fmt == "%d/%m/%Y":
                parsed = datetime.strptime(match.group(0), fmt)
            elif fmt == "day_first":
                day = int(match.group(1))
                month_text = match.group(2).lower()[:3]
                month = next((num for name, num in month_map.items() if name.startswith(month_text)), None)
                if not month: continue
                year = now.year
                parsed = datetime(year, month, day)
                if parsed.date() < now.date(): parsed = parsed.replace(year=year + 1)
            elif fmt == "month_first":
                month_text = match.group(1).lower()[:3]
                day = int(match.group(2))
                month = next((num for name, num in month_map.items() if name.startswith(month_text)), None)
                if not month: continue
                year = now.year
                parsed = datetime(year, month, day)
                if parsed.date() < now.date(): parsed = parsed.replace(year=year + 1)
            else:
                continue
            return parsed.replace(hour=23, minute=59, second=0, microsecond=0, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            continue
    return None
