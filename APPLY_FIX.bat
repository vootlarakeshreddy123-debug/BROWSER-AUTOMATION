@echo off
setlocal
cd /d "%~dp0"
echo Browser Automation Agent - fixed navigation edition
 echo.
echo This package replaces source files only.
echo Existing frontend node_modules is NOT touched.
echo You do NOT need npm install if node_modules already exists.
echo.
echo Backend: use Python 3.12
 echo   cd backend
 echo   py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000
 echo.
echo Frontend: in another terminal
 echo   cd frontend
 echo   npm run dev
 echo.
pause
