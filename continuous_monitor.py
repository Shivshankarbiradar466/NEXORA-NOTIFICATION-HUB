"""Continuous Gmail Inbox Auto-Monitoring Service.

Runs a lightweight, resilient in-process background daemon thread that
continuously scans incoming unread/recent Gmail messages for all users who have
monitoring enabled, without requiring Celery beat or manual button clicks.

Also provides debounce locks and state tracking to prevent concurrent or
duplicate scans across the web thread, API poller, and Celery tasks.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_monitor_thread = None
_stop_event = threading.Event()
_user_scan_lock = threading.Lock()
_last_user_scan = {}  # user_id -> float (monotonic or epoch time)
_active_user_scans = set()  # set of user_ids currently being scanned


def can_scan_user(user_id: int, min_interval: float = 12.0) -> bool:
    """Return True if the user is not currently scanning and min_interval has elapsed."""
    with _user_scan_lock:
        if user_id in _active_user_scans:
            return False
        last_time = _last_user_scan.get(user_id, 0.0)
        return (time.time() - last_time) >= min_interval


def record_user_scan(user_id: int):
    """Mark the scan timestamp for a user."""
    with _user_scan_lock:
        _last_user_scan[user_id] = time.time()


def acquire_user_scan(user_id: int, min_interval: float = 12.0) -> bool:
    """Atomically check and acquire scan lock for user_id."""
    with _user_scan_lock:
        if user_id in _active_user_scans:
            return False
        last_time = _last_user_scan.get(user_id, 0.0)
        if (time.time() - last_time) < min_interval:
            return False
        _active_user_scans.add(user_id)
        return True


def release_user_scan(user_id: int):
    """Release scan lock and record scan timestamp."""
    with _user_scan_lock:
        _active_user_scans.discard(user_id)
        _last_user_scan[user_id] = time.time()


def scan_user_inbox_safe(user_id: int, app, min_interval: float = 12.0) -> int:
    """Scan inbox for user_id safely with debounce and locking.

    Returns the count of new emails imported.
    """
    if not acquire_user_scan(user_id, min_interval=min_interval):
        return 0

    from .. import db
    from ..models import User
    from .email_service import monitor_inbox

    try:
        with app.app_context():
            user = db.session.get(User, user_id)
            if not user or not user.monitoring_enabled or not user.google_sub:
                return 0
            new_count = monitor_inbox(user)
            return new_count
    except Exception as exc:
        logger.warning("Safe inbox scan error for user %s: %s", user_id, exc)
        return 0
    finally:
        release_user_scan(user_id)
        try:
            from .. import db
            db.session.remove()
        except Exception:
            pass


def _continuous_monitor_worker(app, check_interval: float = 15.0):
    """Continuous background worker loop."""
    logger.info("Continuous Gmail Auto-Monitoring worker started (interval: %.1fs).", check_interval)
    last_reminder_sweep = 0.0

    while not _stop_event.is_set():
        try:
            from .. import db
            from ..models import User

            with app.app_context():
                try:
                    active_users = (
                        User.query.filter_by(monitoring_enabled=True)
                        .filter(User.google_sub.isnot(None))
                        .all()
                    )
                    user_ids = [u.id for u in active_users]
                except Exception as db_exc:
                    logger.warning("Continuous monitor: DB query error: %s", db_exc)
                    user_ids = []
                finally:
                    db.session.remove()

            for uid in user_ids:
                if _stop_event.is_set():
                    break
                if can_scan_user(uid, min_interval=check_interval):
                    try:
                        added = scan_user_inbox_safe(uid, app, min_interval=check_interval)
                        if added > 0:
                            logger.info("[Continuous Monitor] User %s: Auto-scanned %d new email(s).", uid, added)
                    except Exception as scan_err:
                        logger.warning("[Continuous Monitor] Scan failed for user %s: %s", uid, scan_err)

            # Sweep task deadline reminders periodically (~every 60s)
            now_ts = time.time()
            if (now_ts - last_reminder_sweep) >= 60.0:
                last_reminder_sweep = now_ts
                try:
                    with app.app_context():
                        from .task_reminder_service import sweep_and_send_task_reminders
                        sweep_and_send_task_reminders(app)
                except Exception as rem_err:
                    logger.warning("Continuous monitor: task reminder sweep error: %s", rem_err)

        except Exception as loop_err:
            logger.warning("[Continuous Monitor] Unexpected error in worker loop: %s", loop_err)

        # Sleep in small slices to allow responsive stop on app exit
        for _ in range(int(check_interval * 2)):
            if _stop_event.is_set():
                break
            time.sleep(0.5)

    logger.info("Continuous Gmail Auto-Monitoring worker stopped.")


def start_continuous_monitor(app, check_interval: float = 15.0):
    """Start the background monitoring thread if not already running."""
    global _monitor_thread

    # Do not start in tests, Celery worker commands, or database migration runs
    if app.config.get("TESTING") or os.environ.get("FLASK_TESTING") == "1":
        return
    if os.environ.get("CELERY_LOADER") or "celery" in sys.argv[0].lower():
        return
    if any(arg in sys.argv for arg in ("db", "migrate", "upgrade", "init")):
        return

    with _user_scan_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return

        _stop_event.clear()
        _monitor_thread = threading.Thread(
            target=_continuous_monitor_worker,
            args=(app, check_interval),
            name="GmailContinuousMonitorThread",
            daemon=True,
        )
        _monitor_thread.start()
        logger.info("Gmail Continuous Auto-Monitoring thread launched successfully.")


def stop_continuous_monitor():
    """Signal background thread to stop."""
    _stop_event.set()
