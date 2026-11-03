@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" (
  echo ERROR: .env is missing.
  echo Copy .env.example to .env and fill in your local credentials first.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Virtual environment not found.
  echo Run: python -m venv .venv
  echo Then: .venv\Scripts\python -m pip install -r requirements.txt
  pause
  exit /b 1
)
start "NEXORA Flask Server" /D "%~dp0" cmd /k ".venv\Scripts\python.exe run.py"
start "NEXORA Celery Worker" /D "%~dp0" cmd /k ".venv\Scripts\python.exe celery_worker.py"
start "NEXORA Celery Beat" /D "%~dp0" cmd /k ".venv\Scripts\python.exe -m celery -A celery_worker.celery beat --loglevel=INFO"
echo NEXORA server, worker, and scheduler windows were opened.
echo Make sure PostgreSQL and Redis/Memurai are running before using Gmail monitoring or task reminders.
endlocal
