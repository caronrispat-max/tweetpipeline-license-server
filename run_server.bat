@echo off
cd /d "%~dp0"
echo Starting Tweet Pipeline Pro License Server...
echo Open http://127.0.0.1:8000/admin to configure it.
python -m uvicorn license_server:app --host 0.0.0.0 --port 8000
pause
