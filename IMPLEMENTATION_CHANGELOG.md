# NEXORA v4.2 — implementation changelog

## Existing design and behavior preserved
- Retained the existing dark NEXORA CSS design system, shared navigation, login/profile/settings pages, Gmail OAuth integration, PostgreSQL/Flask structure, spam controls, WhatsApp notification workflow, and existing dashboard components.
- New Ask NEXORA page reuses `base.html`, existing card/button styles, and the current dashboard visual language. No wholesale redesign was performed.

## Added and completed in this update
1. **Ask NEXORA assistant:** account-scoped search over email sender/subject/body/summary/category, extracted attachment text, spam/phishing results, and tasks. Shows source summaries and links to the original stored email. Does not require a paid LLM API key; explicitly states when content is not in synced records.
2. **Attachment/document intelligence:** stores attachment metadata and safely extracts text from PDF, DOCX, TXT, CSV, and HTML within a 5 MiB limit. Supports bounded parsing, records extraction status, does not execute files, and does not store binary attachment contents locally.
3. **Full-label Gmail sync:** initial paginated `in:anywhere` scan includes Sent, Drafts, Spam, and Trash labels. Label/read/trash states are refreshed for existing records in the recent rolling 30-day window. Existing records can be backfilled with attachment metadata/text and missing task suggestions.
4. **Mailbox browsing and search:** All Mail, Inbox, Sent, Drafts, Promotions, Social, Updates, Spam & Quarantine, Suspicious/Phishing, and separate Trash view. Server-side search applies before 50-message pagination so records beyond the first page remain searchable.
5. **Task/deadline manager:** attachment text can also produce task suggestions; suggested tasks must be accepted before reminders are enabled. Added one-time WhatsApp reminders for accepted tasks due within 24 hours or up to 24 hours overdue.
6. **Scheduled automation:** updated Windows startup script to start Flask, Celery worker, and Celery Beat separately so monitoring and task reminder schedules can run.
7. Added a database migration for Gmail labels, attachment records, and task reminder state; updated setup and feature documentation.

## Important limitations
- Ask NEXORA is a grounded local retrieval assistant, not an unrestricted generative AI chatbot. It only answers from records already synchronized into the signed-in user's account.
- The initial Gmail sync may take a long time for a large mailbox. Subsequent scheduled scans refresh the most recent 30 days; this is not a Gmail History API change-stream implementation.
- PDF/DOCX extraction may fail for encrypted files, image-only scans, malformed documents, or unsupported formats. OCR and malware scanning are not included.
- The spam training dataset is a tiny synthetic starter set. Phishing checks are heuristic. Neither should be described as production-grade without representative data and independent evaluation.
- NEXORA sender blocking is an application-level rule; it does not automatically create Gmail server-side filters.
- WhatsApp reminders require valid Twilio configuration, a configured recipient, and a running Celery worker and Beat process. A reminder is marked sent only when Twilio accepts it; provider acceptance is not the same as handset delivery.

## Validation performed in this build environment
- Python syntax compilation passed.
- Jinja syntax parsing passed for all 17 HTML templates.
- Nine focused pure-Python unit tests passed (five email-intelligence tests and four attachment-service tests).
- Full Flask boot, live PostgreSQL migration, Gmail OAuth/API, Redis/Celery scheduling, and Twilio delivery were not tested here because the runtime dependencies and user-specific services/credentials are unavailable in this environment.
