import base64
import logging
import re
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr

from googleapiclient.discovery import build
from ..auth import _get_gmail_credentials
from .. import db
from ..models import EmailMessage, BlockedSender, TrustedSender, EmailTask, EmailAttachment
from .email_intelligence import analyse_email, extract_action_items
from .summarizer import summarize_email
from .attachment_service import decode_gmail_data, extract_text, SUPPORTED_TEXT_TYPES, MAX_EXTRACT_BYTES

logger = logging.getLogger(__name__)

def _gmail_service(user):
    return build("gmail", "v1", credentials=_get_gmail_credentials(user), cache_discovery=False)

def _headers(message):
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}

def _html_to_plain_text(content):
    if not content or "<" not in content:
        return content or ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        import re
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    except Exception:
        import re
        return re.sub(r"<[^>]+>", " ", content).strip()

def _body(payload):
    # If there are parts, preferentially check for html or plain text
    data = payload.get("body", {}).get("data")
    if data:
        try:
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
        except Exception:
            return ""
    parts = payload.get("parts", []) or []
    # If multiple parts, check if there is an html part or plain text
    html_body = ""
    plain_body = ""
    for part in parts:
        mime = part.get("mimeType", "")
        extracted = _body(part)
        if extracted:
            if "html" in mime.lower():
                html_body = extracted
            elif "plain" in mime.lower() and not plain_body:
                plain_body = extracted
            elif not html_body:
                html_body = extracted
    return html_body or plain_body or ""

def _classify(text):
    try:
        import joblib
        from flask import current_app
        model = joblib.load(current_app.config["ML_MODEL_PATH"])
        category = str(model.predict([text])[0])
        confidence = float(max(model.predict_proba([text])[0])) if hasattr(model, "predict_proba") else 0.0
    except Exception:
        category, confidence = "General", 0.0

    low = text.lower()
    if any(x in low for x in ("urgent", "immediately", "critical", "asap", "deadline today")):
        priority = "Urgent"
    elif any(x in low for x in ("deadline", "invoice", "payment", "interview", "meeting")):
        priority = "Important"
    else:
        priority = "Normal"

    return {"category": category, "priority": priority, "confidence": confidence}

def _confidentiality(subject, body, sender):
    text = f"{subject} {body} {sender}".lower()
    high = ("password", "otp", "one-time password", "bank account", "credit card",
            "api key", "secret key", "private key", "salary", "medical record")
    medium = ("invoice", "contract", "internal", "employee", "customer", "candidate",
              "hr", "confidential", "assessment", "payroll")
    if any(k in text for k in high):
        return "Highly Confidential"
    if any(k in text for k in medium):
        return "Confidential"
    if any(k in text for k in ("team", "project", "meeting", "work")):
        return "Internal"
    return "General"

def _sender_key(sender):
    if not sender:
        return ""
    _, address = parseaddr(str(sender))
    return address.strip().lower()

def _extract_whatsapp_link(body):
    """Helper function to safely scan the email body for a phone number."""
    if not body:
        return ""
    
    # Regex looks for keywords followed by a 10-15 digit number
    pattern = r"(?i)(?:phone|mobile|mob|whatsapp|call|tel)[\s:.-]*(\+?\d[\d\s.-]{8,14}\d)"
    match = re.search(pattern, body)
    
    if match:
        raw_number = match.group(1)
        clean_number = re.sub(r"[\s\-\(\)]", "", raw_number)
        
        # Add India country code if exactly 10 digits
        if len(clean_number) == 10:
            clean_number = f"+91{clean_number}"
            
        wa_number = clean_number.replace("+", "")
        return f"https://wa.me/{wa_number}"
        
    return ""

def _queue_whatsapp(user, sender, subject, category, priority, confidence, confidentiality, summary, body, tasks=None):
    """Send or queue an email alert with extracted tasks and deadlines, and sender contact links."""
    if not user.whatsapp_number:
        return {"queued": False, "reason": "WhatsApp number not configured"}

    from urllib.parse import quote

    sender_name, sender_email = parseaddr(sender)
    sender_display = sender_name or sender_email or "Unknown sender"

    # Auto-scan the email body for a phone number
    whatsapp_link = _extract_whatsapp_link(body)

    # Include a separate Gmail compose link as a fallback/contact option.
    email_link = ""
    if sender_email:
        email_link = (
            "https://mail.google.com/mail/?view=cm&fs=1"
            f"&to={quote(sender_email, safe='')}"
            f"&su={quote('Re: ' + (subject or ''), safe='')}"
        )

    message = (
        "NEXORA - NEW EMAIL\n\n"
        f"From: {sender_display[:180]}\n"
        f"Sender email: {sender_email or 'Unavailable'}\n"
        f"Subject: {(subject or '(No Subject)')[:220]}\n"
        f"Category: {category}\n"
        f"Priority: {priority}\n"
        f"Confidentiality: {confidentiality}\n"
        f"Classifier confidence: {confidence:.0%}\n\n"
        f"Summary: {(summary or '')[:500]}"
    )

    # Include action items & deadlines if present in email or attachments
    if tasks:
        task_lines = []
        for t in tasks:
            t_title = getattr(t, "title", None) or getattr(t, "source_text", None) or str(t)
            t_due = getattr(t, "due_at", None)
            if t_due:
                due_str = t_due.strftime("%d %b %Y, %I:%M %p")
                task_lines.append(f"• {t_title[:220]}\n  ⏰ Deadline: {due_str} (Action Required)")
            else:
                task_lines.append(f"• {t_title[:220]}")
        if task_lines:
            message += "\n\n📋 TASKS & DEADLINES TO DO:\n" + "\n".join(task_lines)
    
    # Only add WhatsApp link if a number was successfully found in the email
    if whatsapp_link:
        message += f"\n\nOPEN WHATSAPP CHAT WITH SENDER:\n{whatsapp_link}"
    
    # The Gmail link is ALWAYS added, regardless of the WhatsApp link
    if email_link:
        message += f"\n\nREPLY BY EMAIL (open Gmail):\n{email_link}"

    # Try direct Twilio send first so alert is delivered immediately even if Celery/Redis is off
    try:
        from .whatsapp import send_whatsapp
        from ..models import NotificationLog
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
        return {"queued": True, "direct_send": True, "result": result}
    except Exception as exc:
        logger.warning("Direct WhatsApp send encountered error, attempting Celery queue: %s", exc)
        try:
            from ..tasks import whatsapp_task
            task = whatsapp_task.delay(user.id, message)
            return {"queued": True, "task_id": task.id}
        except Exception as q_exc:
            logger.exception("Could not queue WhatsApp notification: %s", q_exc)
            return {"queued": False, "reason": str(q_exc)}

def _attachment_parts(payload):
    """Yield named attachment MIME parts recursively."""
    if not isinstance(payload, dict):
        return
    filename = (payload.get("filename") or "").strip()
    body = payload.get("body") or {}
    mime_type = payload.get("mimeType") or "application/octet-stream"
    if filename:
        yield {
            "filename": filename[:500],
            "mime_type": mime_type[:255],
            "attachment_id": body.get("attachmentId"),
            "size": int(body.get("size") or 0),
            "inline_data": body.get("data"),
        }
    for part in payload.get("parts") or []:
        yield from _attachment_parts(part)


def _has_attachment(payload):
    return next(_attachment_parts(payload), None) is not None


def _save_attachments(service, user, row, message):
    payload = message.get("payload") or {}
    for meta in _attachment_parts(payload):
        filename = meta["filename"]
        existing = EmailAttachment.query.filter_by(
            user_id=user.id, gmail_message_id=row.message_id, filename=filename
        ).first()
        if existing:
            continue
        mime_type = meta["mime_type"]
        size = meta["size"]
        status = "unsupported_type"
        extracted = ""
        is_supported = mime_type.lower() in SUPPORTED_TEXT_TYPES or filename.lower().endswith((".txt", ".md", ".log", ".csv", ".pdf", ".docx", ".html", ".htm"))
        if is_supported and size <= MAX_EXTRACT_BYTES:
            try:
                data = meta.get("inline_data")
                if not data and meta.get("attachment_id"):
                    response = service.users().messages().attachments().get(
                        userId="me", messageId=row.message_id, id=meta["attachment_id"]
                    ).execute()
                    data = response.get("data")
                raw = decode_gmail_data(data)
                extracted, status = extract_text(filename, mime_type, raw)
            except Exception:
                logger.exception("Could not extract attachment text for message %s", row.message_id)
                status = "extraction_failed"
        elif size > MAX_EXTRACT_BYTES:
            status = "skipped_too_large"
        db.session.add(EmailAttachment(
            user_id=user.id,
            email_id=row.id,
            gmail_message_id=row.message_id,
            gmail_attachment_id=meta.get("attachment_id"),
            filename=filename,
            mime_type=mime_type,
            size_bytes=size,
            extracted_text=extracted,
            extraction_status=status,
        ))
        # Documents may contain action items too. Keep them as suggestions, not
        # confirmed tasks, and deduplicate against email-body suggestions.
        if extracted and not row.is_spam and (row.phishing_score or 0) < 0.45:
            from .email_intelligence import extract_due_date
            for suggestion in extract_action_items(filename, extracted):
                title = suggestion[:500]
                task_due = extract_due_date(suggestion) or extract_due_date(filename)
                if not EmailTask.query.filter_by(user_id=user.id, email_id=row.id, title=title).first():
                    db.session.add(EmailTask(
                        user_id=user.id, email_id=row.id, title=title,
                        due_at=task_due,
                        source_text=f"From attachment {filename}: {suggestion}",
                        status="open",
                    ))


def monitor_inbox(user):
    """Synchronize Gmail labels and message metadata; extract safe attachment text.

    Scans every unread email once and synchronizes recent incoming messages.
    Deduplicates by (user_id, Gmail message ID) so every incoming email is scanned
    and stored only once.
    """
    service = _gmail_service(user)
    initial_sync = not bool(getattr(user, "gmail_initial_sync_complete", False))
    query = "in:anywhere" if initial_sync else "in:anywhere (is:unread OR newer_than:7d)"
    max_to_sync = 100 if initial_sync else 50

    items = []
    page_token = None
    while True:
        try:
            req = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=min(100, max_to_sync - len(items)),
                pageToken=page_token,
            )
            result = req.execute()
        except Exception as exc:
            logger.warning("Failed to fetch Gmail message list for user %s: %s", user.id, exc)
            break

        items.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token or len(items) >= max_to_sync:
            break

    blocked = {x.sender_key.lower() for x in BlockedSender.query.filter_by(user_id=user.id, match_type="email").all()}
    blocked_domains = {x.sender_key.lower() for x in BlockedSender.query.filter_by(user_id=user.id, match_type="domain").all()}
    trusted = {x.sender_key.lower() for x in TrustedSender.query.filter_by(user_id=user.id, match_type="email").all()}
    trusted_domains = {x.sender_key.lower() for x in TrustedSender.query.filter_by(user_id=user.id, match_type="domain").all()}
    had_failures = False
    new_added_count = 0

    all_mids = [it.get("id") for it in items if it.get("id")]
    existing_mids = set(
        r[0] for r in EmailMessage.query.filter(
            EmailMessage.user_id == user.id,
            EmailMessage.message_id.in_(all_mids)
        ).with_entities(EmailMessage.message_id).all()
    ) if all_mids else set()

    # Prioritize NEW incoming messages first so they are processed immediately
    new_items = [it for it in items if it.get("id") and it.get("id") not in existing_mids]
    existing_items = [it for it in items if it.get("id") and it.get("id") in existing_mids]

    for item in new_items:
        mid = item.get("id")
        try:
            message = service.users().messages().get(userId="me", id=mid, format="full").execute()
        except Exception:
            had_failures = True
            logger.exception("Failed to download Gmail message %s", mid)
            continue

        labels = set(message.get("labelIds", []))
        h = _headers(message)
        subject = h.get("subject", "(No Subject)")
        sender = h.get("from", "")
        recipient = h.get("to", "")
        body = _body(message.get("payload", {})).strip() or "(No readable email body)"
        plain_body = _html_to_plain_text(body) or body
        ai = _classify(subject + "\n" + plain_body)
        summary = summarize_email(subject, plain_body)
        confidentiality = _confidentiality(subject, plain_body, sender)
        sender_address = _sender_key(sender)
        sender_domain = sender_address.rsplit("@", 1)[-1] if "@" in sender_address else ""
        intelligence = analyse_email(
            sender, subject, plain_body,
            blocked=(sender_address in blocked or sender_domain in blocked_domains),
            trusted=(sender_address in trusted or sender_domain in trusted_domains),
        )
        user_override = None
        if sender_address in blocked or sender_domain in blocked_domains:
            user_override = "spam"
        elif sender_address in trusted or sender_domain in trusted_domains:
            user_override = "not_spam"
        is_spam = user_override == "spam" or (user_override != "not_spam" and ("SPAM" in labels or intelligence["is_spam"]))
        action_suggestions = [] if is_spam or intelligence["phishing_score"] >= 0.45 else extract_action_items(subject, plain_body)
        received_at = None
        if message.get("internalDate"):
            try:
                received_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)
            except Exception:
                pass
        row = EmailMessage(
            user_id=user.id, message_id=mid, sender=sender, recipient=recipient,
            subject=subject, body=body, category=ai["category"], priority=ai["priority"],
            confidence=ai["confidence"], confidentiality=confidentiality, summary=summary,
            status="received", received_at=received_at, is_spam=is_spam,
            spam_score=intelligence["spam_score"],
            spam_reason=("Sender is on your blocklist" if user_override == "spam" else ("Gmail marked this message as spam" if "SPAM" in labels else intelligence["spam_reason"])),
            phishing_score=intelligence["phishing_score"], phishing_reason=intelligence["phishing_reason"],
            user_override=user_override, is_read=("UNREAD" not in labels),
            has_attachments=_has_attachment(message.get("payload", {})),
            gmail_labels=",".join(sorted(labels)), is_trashed=("TRASH" in labels),
        )
        if not is_spam and intelligence["phishing_score"] < 0.45:
            try:
                from .reply_generator import generate_ai_reply
                row.ai_reply = generate_ai_reply(row, user)
            except Exception:
                pass
        try:
            db.session.add(row)
            db.session.flush()
            _save_attachments(service, user, row, message)
            tasks_created = []
            for suggestion in action_suggestions:
                from .email_intelligence import extract_due_date
                title = suggestion[:500]
                task_due = extract_due_date(suggestion) or extract_due_date(subject)
                existing_t = EmailTask.query.filter_by(user_id=user.id, email_id=row.id, title=title).first()
                if not existing_t:
                    new_t = EmailTask(
                        user_id=user.id, email_id=row.id, title=title,
                        due_at=task_due, source_text=suggestion,
                        status="open",
                    )
                    db.session.add(new_t)
                    tasks_created.append(new_t)
                else:
                    tasks_created.append(existing_t)
            db.session.commit()
            new_added_count += 1
        except Exception:
            db.session.rollback()
            had_failures = True
            logger.exception("Failed to store email %s", mid)
            continue

        recent_enough_for_initial_alert = bool(received_at and received_at >= datetime.now(timezone.utc) - timedelta(hours=24))
        notify_allowed = (not initial_sync) or recent_enough_for_initial_alert
        # Send WhatsApp alert for all incoming emails except spam (and except Trash/Draft/Sent)
        if notify_allowed and not is_spam and not (labels & {"TRASH", "DRAFT", "SENT"}):
            all_email_tasks = EmailTask.query.filter_by(user_id=user.id, email_id=row.id).all() or tasks_created
            _queue_whatsapp(
                user, sender, subject, ai["category"], ai["priority"],
                ai["confidence"], confidentiality, summary, plain_body,
                tasks=all_email_tasks,
            )

    # In routine scans, refresh labels for recent existing emails using minimal format
    existing_to_check = existing_items if initial_sync else existing_items[:15]
    if existing_to_check:
        updated_any = False
        for item in existing_to_check:
            mid = item.get("id")
            try:
                message = service.users().messages().get(userId="me", id=mid, format="minimal").execute()
                labels = set(message.get("labelIds", []))
                existing = EmailMessage.query.filter_by(user_id=user.id, message_id=mid).first()
                if existing:
                    existing.gmail_labels = ",".join(sorted(labels))
                    existing.is_read = "UNREAD" not in labels
                    existing.is_trashed = "TRASH" in labels
                    if "SPAM" in labels and existing.user_override != "not_spam":
                        existing.is_spam = True
                        existing.spam_reason = existing.spam_reason or "Gmail marked this message as spam"
                    elif "SPAM" not in labels and existing.user_override is None and existing.spam_reason == "Gmail marked this message as spam":
                        existing.is_spam = False
                        existing.spam_reason = None
                    updated_any = True
            except Exception:
                logger.warning("Could not refresh labels for existing message %s", mid)
                continue
        if updated_any:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

    if initial_sync and not had_failures:
        user.gmail_initial_sync_complete = True
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    if getattr(user, "auto_reply_enabled", False):
        try:
            from .auto_reply_service import process_auto_ai_replies
            process_auto_ai_replies(user_id=user.id)
        except Exception:
            pass

    return new_added_count

def send_gmail_reply(user, message_id, reply_text, attachments=None):
    reply_text = (reply_text or "").strip()
    if not reply_text:
        raise ValueError("Reply cannot be empty.")
    service = _gmail_service(user)
    original = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "Subject", "Message-ID", "References"]
    ).execute()
    headers = _headers(original)
    sender = headers.get("from", "")
    _, to = parseaddr(sender)
    subject = headers.get("subject", "")
    subject = subject if subject.lower().startswith("re:") else "Re: " + subject

    import mimetypes
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    if attachments:
        mime = MIMEMultipart()
        mime.attach(MIMEText(reply_text, "plain", "utf-8"))
        for att in attachments:
            fname = getattr(att, "filename", None) or "attachment"
            fname = os.path.basename(fname)
            if not fname:
                continue
            ctype = getattr(att, "content_type", None) or mimetypes.guess_type(fname)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("application", "octet-stream")
            part = MIMEBase(maintype, subtype)
            data = att.read() if hasattr(att, "read") else att
            if hasattr(att, "seek"):
                att.seek(0)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
            mime.attach(part)
    else:
        mime = MIMEText(reply_text, "plain", "utf-8")

    mime["To"] = to or sender
    mime["Subject"] = subject
    if headers.get("message-id"):
        mime["In-Reply-To"] = headers["message-id"]
    if headers.get("references"):
        mime["References"] = headers["references"]

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    return service.users().messages().send(
        userId="me", body={"raw": raw, "threadId": original.get("threadId")}
    ).execute()