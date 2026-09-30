@echo off
REM Fleet monitor, Windows side -- the published module, not the old script.
REM Close this window or press q to quit. Resize it freely: the layout
REM re-solves every frame, and j/k scrollback works at any size.
start "fleetmon" "%LOCALAPPDATA%\Python\pythoncore-3.11-64\Scripts\fleetmon.exe" %*