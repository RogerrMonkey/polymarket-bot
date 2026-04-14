@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-daily-paper-loop.ps1"
endlocal
