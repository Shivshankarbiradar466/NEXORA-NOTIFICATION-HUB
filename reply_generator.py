"""Contextual AI Email Reply Generator.

Analyzes the full context, tone, subject, and body of incoming emails to generate
articulate, professional, and helpful replies. Supports optional external LLM
providers (Gemini / OpenAI) with automatic zero-dependency local NLP fallback.
"""

import os
import re
from email.utils import parseaddr
from .summarizer import _strip_html


def _extract_first_name(sender_str):
    name, addr = parseaddr(sender_str or "")
    if name:
        parts = [p.strip() for p in name.split() if p.strip()]
        if parts:
            first = parts[0].strip("',\"")
            if len(first) > 1 and first.isalpha():
                return first
    if addr and "@" in addr:
        local = addr.split("@")[0].split(".")[0]
        if local and local.isalpha() and len(local) > 1:
            return local.capitalize()
    return None


def _clean_subject(subject):
    s = (subject or "").strip()
    s = re.sub(r"^(?:(?:re|fwd|fw):\s*)+", "", s, flags=re.IGNORECASE).strip()
    return s or "your inquiry"


def _extract_key_questions_or_requests(text):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    questions = []
    actions = []
    for s in sentences:
        s_clean = s.strip()
        if not s_clean:
            continue
        if "?" in s_clean and len(s_clean) > 8:
            questions.append(s_clean)
        elif re.search(r"\b(?:please|could you|kindly|can you|need you to|would like to)\b", s_clean, re.I):
            actions.append(s_clean)
    return questions[:2], actions[:2]


def _call_gemini_api(prompt, api_key):
    try:
        import urllib.request
        import json

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 400}
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None


def _call_openai_api(prompt, api_key):
    try:
        import urllib.request
        import json

        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a professional email assistant. Write concise, polite, natural, and helpful email replies."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 400,
            "temperature": 0.4
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return res_json["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def generate_ai_reply(email_message, user=None):
    """Generate a context-aware email reply reading the full email subject and body."""
    subject_raw = email_message.subject or ""
    body_raw = email_message.body or ""
    clean_body = _strip_html(body_raw).strip()
    clean_subj = _clean_subject(subject_raw)
    sender = email_message.sender or ""
    first_name = _extract_first_name(sender)
    greeting = f"Hi {first_name}," if first_name else "Hello,"

    user_signature = (user.username if user and user.username else "NEXORA Assistant")

    # 1. Try LLM if configured in environment
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    prompt = (
        f"You are drafting an email reply on behalf of {user_signature}.\n"
        f"Read this incoming email carefully and write a polite, contextually relevant, and complete response.\n\n"
        f"From: {sender}\n"
        f"Subject: {subject_raw}\n"
        f"Body:\n{clean_body[:2500]}\n\n"
        f"Guidelines:\n"
        f"- Start with '{greeting}'\n"
        f"- Directly address the specific topic, request, or questions raised in the email.\n"
        f"- Keep the tone professional, helpful, and concise (2-4 paragraphs).\n"
        f"- Sign off with 'Best regards,\n{user_signature}'."
    )

    if gemini_key:
        llm_reply = _call_gemini_api(prompt, gemini_key)
        if llm_reply:
            return llm_reply

    if openai_key:
        llm_reply = _call_openai_api(prompt, openai_key)
        if llm_reply:
            return llm_reply

    # 2. Advanced Local NLP Contextual Reply Synthesis
    questions, actions = _extract_key_questions_or_requests(clean_body)
    low_body = clean_body.lower()
    low_subj = clean_subj.lower()

    # Determine intent & context
    is_meeting = any(k in low_body or k in low_subj for k in [
        "meeting", "schedule", "call", "availability", "time to chat", "calendar", "zoom", "google meet", "teams"
    ])
    is_document_or_review = any(k in low_body or k in low_subj for k in [
        "attached", "attachment", "document", "draft", "report", "review", "proposal", "contract", "file"
    ])
    is_urgent = any(k in low_body or k in low_subj for k in [
        "urgent", "asap", "emergency", "critical", "blocking", "immediate attention"
    ])
    is_status_check = any(k in low_body or k in low_subj for k in [
        "status", "update", "following up", "checking in", "progress", "any news"
    ])
    is_gratitude = any(k in low_body for k in [
        "thank you", "thanks for", "appreciate your help", "great work"
    ]) and len(clean_body) < 160

    paragraphs = [greeting, ""]

    if is_gratitude:
        paragraphs.append(f"Thank you for reaching out! You're very welcome, and I'm glad we could assist with \"{clean_subj}\".")
        paragraphs.append("Please don't hesitate to reach back out if there's anything else you need.")
    elif is_urgent:
        paragraphs.append(f"Thank you for bringing this urgent matter regarding \"{clean_subj}\" to my attention.")
        paragraphs.append("I have prioritized your request and am reviewing the details immediately to ensure it is addressed as quickly as possible.")
        if actions:
            paragraphs.append(f"Regarding: \"{actions[0]}\" - this is currently in progress, and I will keep you posted as soon as it is resolved.")
        else:
            paragraphs.append("I will follow up shortly with a comprehensive update once this is resolved.")
    elif is_meeting:
        paragraphs.append(f"Thank you for reaching out regarding \"{clean_subj}\".")
        paragraphs.append("I would be glad to connect. I am reviewing my calendar and availability, and will propose a few suitable time slots for our discussion shortly.")
        paragraphs.append("If there is any preliminary agenda or background material to review beforehand, please feel free to send it over.")
    elif is_document_or_review:
        paragraphs.append(f"Thank you for sharing the details regarding \"{clean_subj}\".")
        paragraphs.append("I confirm receipt of the information and documentation. I am currently reviewing the contents and will provide you with feedback and next steps shortly.")
        if questions:
            paragraphs.append(f"I will also address your inquiry ({questions[0]}) in my follow-up.")
    elif is_status_check:
        paragraphs.append(f"Thank you for following up on \"{clean_subj}\".")
        paragraphs.append("I am actively working on this and finalizing the latest progress. Everything is moving forward, and I expect to provide you with a full status update shortly.")
    else:
        # General professional acknowledgment tailored to the email
        paragraphs.append(f"Thank you for your email regarding \"{clean_subj}\".")
        if actions:
            paragraphs.append(f"I have received your request regarding: \"{actions[0]}\". I am reviewing this now and will follow up with the required details shortly.")
        elif questions:
            paragraphs.append(f"I have noted your questions and am gathering the necessary details to provide you with a comprehensive response shortly.")
        else:
            paragraphs.append("I have received your message and wanted to confirm that it is being processed. I will follow up with you as soon as I have an update.")

    paragraphs.append("")
    paragraphs.append("Best regards,")
    paragraphs.append(user_signature)

    return "\n".join(paragraphs)
