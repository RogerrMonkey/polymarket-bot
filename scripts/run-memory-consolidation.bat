@echo off
setlocal
set ROOT=%~dp0..
py -3 "%ROOT%\skills\consolidate-memory\consolidate_memory.py" --workspace-root "%ROOT%" --hours 24 --recent-window-hours 48
endlocal
