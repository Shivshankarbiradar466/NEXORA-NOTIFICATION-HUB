
from app import create_app
from app.celery_app import celery

# Create the Flask application
app = create_app()

# Load Flask configuration into Celery
celery.conf.update(app.config)


# Run Celery worker on Windows
if __name__ == "__main__":
    celery.worker_main([
        "worker",
        "--loglevel=INFO",
        "--pool=solo",
    ])

