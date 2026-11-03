# Windows quick start

1. Extract the ZIP to a normal writable folder (not inside `Program Files`).
2. Copy `.env.example` to `.env` and fill the real local PostgreSQL, Flask, Google OAuth, SMTP, and Twilio values. Do not share `.env`.
3. Open PowerShell in the extracted project root:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   python train_spam_model.py
   python train_model.py
   ```
4. Start PostgreSQL and Redis/Memurai. Confirm that `DATABASE_URL` points to your existing `notification_hub` database.
5. Back up the database and inspect migration state:
   ```powershell
   python -m flask --app run.py db current
   python -m flask --app run.py db heads
   ```
6. Only if the database's current revision belongs to the supplied migration chain, run:
   ```powershell
   python -m flask --app run.py db upgrade
   ```
7. Run the focused tests:
   ```powershell
   python -m pytest -q
   ```
8. Double-click `START_NEXORA.bat`. It opens the Flask app and Celery worker in separate command windows. Keep PostgreSQL and Redis/Memurai running.
9. Connect Google, configure WhatsApp, run a manual scan, then enable automatic monitoring.

The app cannot start until real local credentials and services are configured. This package deliberately contains no `.env` file or private profile photos.

## New features in this build

1. After activating the virtual environment, run `pip install -r requirements.txt` to install PDF/DOCX text extraction support.
2. Back up PostgreSQL before updating the schema. Run `python -m flask --app run.py db upgrade`.
3. Start Redis/Memurai, then run the Flask server and the Celery worker with beat enabled. Beat is required for scheduled Gmail monitoring and task reminders.
4. Connect Gmail and run the initial sync. The initial full mailbox import can take time. Review **All Mail**, **Sent Mail**, **Drafts**, **Promotions**, **Spam & Quarantine**, and **Trash** after it completes.
5. Ask NEXORA only searches synced mail belonging to the current account. Attachment extraction is bounded to supported formats and 5 MiB per file; encrypted/scanned PDFs may not produce readable text.
6. To activate task reminders, accept a task in **Tasks & Deadlines**, configure WhatsApp, and keep Celery worker/beat running.
