@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-outcome-resolver.ps1"
endlocal
