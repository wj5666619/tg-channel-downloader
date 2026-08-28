@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d %~dp0
set "PY_RUNTIME=%~dp0\runtime\python\python.exe"
if not exist "%PY_RUNTIME%" (
  echo ERROR: runtime not found at %PY_RUNTIME%
  pause
  exit /b 1
)
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
"%PY_RUNTIME%" downloader.py
pause