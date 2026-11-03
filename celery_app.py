
import os

from celery import Celery
from dotenv import load_dotenv

# Load environment variables before configuring Celery.
load_dotenv()

REDIS_URL = (
    os.getenv("CELERY_BROKER_URL")
    or os.getenv("REDIS_URL")
    or "redis://localhost:6379/0"
)

celery = Celery(
    "nexora_notification_hub",
    broker=REDIS_URL,
    backend=os.getenv("CELERY_RESULT_BACKEND", REDIS_URL),
    include=["app.tasks"],
)

celery.conf.update(
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_track_started=True,
    beat_schedule={
        "monitor-enabled-users-every-60-seconds": {
            "task": "app.tasks.monitor_enabled_users",
            "schedule": 60.0,
        },
        "send-due-task-reminders": {
            "task": "app.tasks.send_due_task_reminders",
            "schedule": 1800.0,
        },
        "sweep-auto-ai-replies-every-2-minutes": {
            "task": "app.tasks.sweep_auto_ai_replies",
            "schedule": 120.0,
        },
    },
)