@echo off
setlocal
set ROOT=%~dp0..
py -3 "%ROOT%\skills\research-scout\promote_new_learnings.py" --workspace-root "%ROOT%"
endlocal
