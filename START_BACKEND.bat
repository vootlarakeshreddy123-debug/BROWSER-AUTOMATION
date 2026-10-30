@echo off
cd /d "%~dp0backend"
py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
