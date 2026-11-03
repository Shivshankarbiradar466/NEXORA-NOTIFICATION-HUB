import re

STOP = {
    "the","and","for","that","this","with","from","your","have","will","are","was",
    "you","our","but","not","has","had","they","their","about","into","please","can"
}

def _strip_html(text):
    if not text or "<" not in text:
        return text or ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", text).strip()

def _sentences(text):
    cleaned = re.sub(r"\s+", " ", _strip_html(text)).strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]

def summarize_email(subject, body, max_sentences=2, max_chars=520):
    clean_body = _strip_html(body)
    sentences = _sentences(clean_body)
    if not sentences:
        return "No readable email content was found."
    if len(sentences) <= max_sentences:
        result = " ".join(sentences)
    else:
        words = re.findall(r"[A-Za-z0-9']+", (subject + " " + clean_body).lower())
        freq = {}
        for w in words:
            if len(w) > 3 and w not in STOP:
                freq[w] = freq.get(w, 0) + 1
        ranked = []
        for idx, sentence in enumerate(sentences):
            score = sum(freq.get(w, 0) for w in re.findall(r"[A-Za-z0-9']+", sentence.lower()))
            ranked.append((score, -idx, sentence))
        chosen = [x[2] for x in sorted(ranked, reverse=True)[:max_sentences]]
        result = " ".join(chosen)
    result = result.strip()
    return result[:max_chars].rstrip() + ("…" if len(result) > max_chars else "")
