# NEXORA Notification Hub — Elite Education Build

NEXORA is a Flask + PostgreSQL + Redis/Celery application that turns unread Gmail messages into a polished dashboard signal and optional WhatsApp alert.

## What changed from the uploaded build

- Single login system for every role; there is no separate HR login.
- Role is displayed beside the user's profile and is stored server-side.
- Custom dark/limed editorial UI; Bootstrap was removed.
- Gmail OAuth uses PKCE and requests fresh Google authentication on each Google sign-in attempt with `prompt=login`.
- Gmail access tokens are encrypted with Fernet-derived application encryption.
- Background monitoring is actually scheduled through Celery Beat.
- WhatsApp alerts contain sender, subject, category, priority, classifier confidence, confidentiality estimate and a short local summary.
- Added confidentiality classification to the stored email record.
- Added a database uniqueness constraint for `(user_id, message_id)` in the model.
- Removed the OpenAI/AI-agent dependency so a quota outage cannot break email ingestion.
- Chat assistant is deterministic and local; it does not send mailbox contents to an external LLM.
- CSRF protection is enabled for the chatbot and all state-changing actions.
- Flask-Limiter uses Redis rather than process-local memory.
- Production-style security headers and safer session settings are included.
- `.env` is excluded from the project package and `.env.example` contains placeholders only.

## Important Google limitation

There is no legitimate code-only method to make a public Gmail application avoid Google's OAuth verification requirements. This application requests `gmail.modify` and `gmail.send`; Gmail documents `gmail.modify` as a restricted scope and `gmail.send` as sensitive. Public use of those scopes can require verification, and restricted-scope server-side handling can trigger additional security-assessment requirements.

For education/local development:
1. Keep the OAuth project in Testing.
2. Add the Google accounts you control as test users.
3. Use `http://127.0.0.1:5000/oauth/google/callback` as an exact redirect URI.
4. Expect Google's unverified/testing flow and test-user restrictions.

For a real public product:
- use a production OAuth consent screen,
- verify the brand/domain,
- submit the required scopes for review,
- follow Google's user-data policy,
- and complete any security assessment Google requires.

Google does not promise a literal password prompt every time. `prompt=login` requests reauthentication, but Google may satisfy that with a password, passkey, 2-step verification, or another supported method.

## Run locally

### 1. Environment
Copy `.env.example` to `.env` and fill real credentials. Never commit `.env`.

### 2. PostgreSQL
Create a database named `notification_hub`.

### 3. Redis
Run Redis locally on port 6379.

### 4. Python
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 5. ML model
```powershell
python train_model.py
```

### 6. Database
For a fresh database, create the PostgreSQL database and run the versioned migrations:
```powershell
flask --app run.py db upgrade
```

For an existing Notification Hub database, back it up first and inspect its current revision:
```powershell
flask --app run.py db current
flask --app run.py db upgrade
```
The supplied migrations include the new security columns. Do not run `db stamp` or generate a replacement migration blindly if your current revision is not part of this migration chain; reconcile the migration history first.

### 7. Start the web server
```powershell
python run.py
```

### 8. Start worker + scheduler
In a second terminal:
```powershell
python celery_worker.py
```

Celery Beat will schedule `monitor_enabled_users` using `MONITOR_INTERVAL_MINUTES`.

## End-to-end flow

Google login -> encrypted OAuth token -> dashboard -> monitoring toggle -> Celery Beat -> Gmail API -> local classifier + summarizer + confidentiality estimator -> PostgreSQL -> WhatsApp Celery task -> Twilio.

The browser does not need to remain open for automatic monitoring.

## Security notes

Rotate any credential that has ever been exposed in a repository, ZIP, screenshot, terminal output or chat. The uploaded project contained credential values in `.env.example`; those credentials should be treated as compromised and replaced before further use.


## Elite UI v4 upgrade

The frontend has been redesigned around the supplied NEXORA reference screenshot:
- fixed dark navy sidebar with NEXORA branding
- premium blue/violet SaaS cards and glass-like surfaces
- Gmail monitoring status banner with live toggle
- email statistics cards
- intelligent recent-email list with confidence/priority/confidentiality badges
- WhatsApp notification preview
- Gmail connection card
- monitoring control card
- profile card
- recent notification table
- global email search with Ctrl+K
- responsive mobile sidebar
- redesigned login page
- existing Flask/PostgreSQL/Gmail OAuth/Celery/WhatsApp routes are retained

The UI reads the existing backend variables (`stats`, `emails`, `notifications`, `user`) rather than hardcoding demo email data.


## Security checklist implementation (v4.1 hardened)

- Login: Flask-Limiter limits login posts to 5 per 15 minutes; failures are logged with masked identity hashes and IP, repeated attempts trigger an arithmetic challenge, and per-account lock state is stored.
- Signup: strict server-side username/email/password validation, unique database constraints, SMTP verification link with a 24-hour expiry, and generic duplicate-account response.
- Sessions: session rotation after login, HttpOnly/SameSite cookies, configurable Secure cookie, one-hour idle expiry, CSRF on state-changing routes, and logout via POST.
- Errors: user-facing auth responses are generic; detailed failures go to server logs.
- Password reset: 30-minute single-use hashed token, generic response, password reset revokes Google connection and stops monitoring.
- MFA: TOTP authenticator setup, login challenge, single-use hashed backup codes, and confirmation-code-protected disable action.
- Backend/API: ORM queries, per-user resource filtering, server-side validation, request size limit, and CSRF.
- Logging: login success/failure and MFA events are recorded; passwords/tokens are not logged.
- Authorization: authenticated routes require login and email/notification queries are scoped to the active user; self-promotion is not allowed.
- Security headers: CSP, nosniff, frame protection, referrer policy, permissions policy, and HSTS on HTTPS.

### New environment settings
SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM, and APP_BASE_URL are required for signup verification mail. Use an SMTP provider/app password; do not use your Google account password. `pyotp` is installed for MFA.

For local testing, set a strong `FLASK_SECRET_KEY`. In HTTPS deployment, set `SESSION_COOKIE_SECURE=true` and `NEXORA_ALLOW_INSECURE_COOKIES=false`. Review CSP if you add external scripts or image hosts.

## NEXORA Email Intelligence additions

This update preserves the existing NEXORA visual theme and adds the following:

- **All Mail / Inbox / Sent / Drafts / Promotions / Social / Updates / Spam & Quarantine / Suspicious** views, server-side search across stored records, and 50-message pagination so larger mailboxes can be browsed without a 500-message display cap.
- **Spam controls**: mark as spam, restore as not spam, block an exact sender, block a sender domain, trust a sender, and unblock senders. These actions apply to NEXORA's own mailbox view and alert rules; they do not automatically create a Gmail server-side filter or report a message to Google.
- **Hybrid spam scoring**: a small scikit-learn TF-IDF + Logistic Regression starter model plus transparent local heuristics. The included training CSV is a demonstration seed set, not a representative production dataset. Do not claim production accuracy based on this small sample.
- **Phishing indicators**: suspicious credential language, unusual hostnames, non-HTTPS links, and possible sender/link mismatches. These are warnings, not proof that a message is malicious.
- **WhatsApp alert suppression** for messages placed in NEXORA Spam or above the configured phishing-risk threshold.
- **Task suggestions** extracted from action-oriented email text, with basic explicit-date / tomorrow / weekday deadline parsing. Review suggestions; dates can be ambiguous.
- **Analytics** based on stored mailbox and notification records.
- **Twilio status webhook** at `/webhooks/twilio/status`, which validates the Twilio signature and updates the matching notification log when configured with a public HTTPS callback URL.
- **Full-label Gmail synchronization**: the first paginated scan imports messages across Gmail labels using `in:anywhere`, including Sent, Drafts, Spam and Trash; Trash is shown in NEXORA's separate Trash view. Later scans refresh the most recent 30 days. The first scan avoids flooding WhatsApp with historical messages (only messages from the last 24 hours may alert). Mailbox sync is limited to the Gmail API records that the account can access.

### Apply the database migration safely

1. Stop the Flask server and Celery worker.
2. Back up PostgreSQL first.
3. From the project root and the correct virtual environment, inspect the current revision:
   ```powershell
   python -m flask --app run.py db current
   python -m flask --app run.py db heads
   ```
4. If the existing database follows this project's migration chain, apply:
   ```powershell
   python -m flask --app run.py db upgrade
   ```
5. If `db current` is not on the expected chain, stop and reconcile the migration history instead of stamping or resetting the database blindly.
6. Train the included starter spam model:
   ```powershell
   python train_spam_model.py
   ```
   Existing classifier training remains available through `python train_model.py`.
7. Start the Flask app and Celery worker, connect Gmail, enable monitoring, and run a manual scan to verify the new tables and views.

### Twilio status callbacks

Set `TWILIO_STATUS_CALLBACK_URL` to the exact public HTTPS URL ending in `/webhooks/twilio/status` in your `.env` and in Twilio's message status callback configuration. A localhost URL cannot receive callbacks from Twilio without a secure public tunnel or deployment. The endpoint rejects callbacks when signature validation is not configured or the signature is invalid.

### Test coverage and limitations

Run the local pure-Python checks with:
```powershell
python -m unittest discover -s tests -v
```

The included tests cover spam/blocklist scoring, phishing indicators, action-item extraction, and basic date parsing. A full end-to-end test still requires the project's installed dependencies, a PostgreSQL database, Redis/Memurai, valid Google OAuth credentials, and Twilio configuration. Validate model quality on a separate labeled test set and report precision, recall, F1-score, and false-positive rate before making academic accuracy claims.

## Ask NEXORA, attachment intelligence and reminders

- Open **Ask NEXORA** from the sidebar to search the signed-in user's synchronized messages, summarize stored inbox records, inspect extracted attachment text, review suspicious messages, and list task suggestions. The assistant is grounded in the local database and falls back without a paid LLM API key; it does not claim to know emails that have not synchronized.
- Gmail sync uses `in:anywhere` for the initial paginated import, including Sent, Drafts, Spam and Trash labels. Later scans refresh messages from the most recent 30 days. Trash remains available in the separate Trash view. Very large mailboxes can take time on the first sync.
- Attachment metadata is saved for named attachments. Text extraction supports PDF, DOCX, TXT, CSV and HTML within a 5 MiB per-file extraction limit. Binary attachment files are not stored locally and unsupported file types are not executed. `pypdf` and `python-docx` are installed through `requirements.txt`.
- Suggested tasks are not automatically treated as confirmed commitments. Open **Tasks & Deadlines** and select **Accept & Remind** to activate a task. Celery Beat checks every 30 minutes and sends one WhatsApp reminder when an accepted task is due within 24 hours or up to 24 hours overdue. WhatsApp credentials and a recipient number must be configured.
- Apply the new database migration using `python -m flask --app run.py db upgrade` after backing up the database. Keep a copy of the current project and database before upgrading.
