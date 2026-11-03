import os
import re
import logging
from flask import current_app
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

logger = logging.getLogger(__name__)

def _cfg(key, default=""):
    return current_app.config.get(key) or os.getenv(key) or default

def normalize_whatsapp_number(number):
    raw = str(number or "").strip()
    if raw.lower().startswith("whatsapp:"):
        raw = raw[9:].strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError("Please provide a valid WhatsApp number with country code.")
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    if digits.startswith("91") and len(digits) == 12:
        return "+" + digits
    if raw.startswith("+") or len(digits) >= 11:
        return "+" + digits
    raise ValueError("Use a valid WhatsApp number with country code, e.g. +919876543210.")

def send_whatsapp(to_number, message):
    try:
        sid = _cfg("TWILIO_ACCOUNT_SID")
        token = _cfg("TWILIO_AUTH_TOKEN")
        sender = _cfg("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        if not sid or not token:
            return {"ok": False, "error": "Twilio settings are incomplete."}
        target = "whatsapp:" + normalize_whatsapp_number(to_number)
        sender = sender if sender.lower().startswith("whatsapp:") else "whatsapp:" + sender
        args = {"body": str(message)[:1500], "from_": sender, "to": target}
        callback = _cfg("TWILIO_STATUS_CALLBACK_URL")
        if callback:
            args["status_callback"] = callback
        msg = Client(sid, token).messages.create(**args)
        return {"ok": True, "sid": msg.sid, "status": msg.status, "to": target}
    except TwilioRestException as exc:
        return {"ok": False, "error": exc.msg, "code": getattr(exc, "code", None)}
    except Exception as exc:
        logger.exception("WhatsApp sending failed")
        return {"ok": False, "error": str(exc)}
