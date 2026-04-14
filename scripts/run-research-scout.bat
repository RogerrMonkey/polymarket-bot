@echo off
setlocal
set ROOT=%~dp0..
py -3 "%ROOT%\skills\research-scout\research_scout.py" --workspace-root "%ROOT%" --hours 24
endlocal
