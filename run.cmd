@echo off
cd /d "%~dp0"
if exist "dist\PDF2MD\PDF2MD.exe" (
  start "" "dist\PDF2MD\PDF2MD.exe" %*
) else (
  ".venv\Scripts\python.exe" run.py %*
)
