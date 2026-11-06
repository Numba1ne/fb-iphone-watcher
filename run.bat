@echo off
REM Restart-on-crash wrapper for the watcher.
REM If the script ever exits (crash, error), wait 30s and start again.
REM Stop it with Ctrl+C twice, or close the window.

cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set PY=.venv\Scripts\python.exe
) else (
    set PY=python
)

:loop
echo [%date% %time%] starting watcher
%PY% main.py
echo [%date% %time%] watcher exited with code %errorlevel%
echo Restarting in 30 seconds... (Ctrl+C to stop)
timeout /t 30 /nobreak >nul
goto loop
