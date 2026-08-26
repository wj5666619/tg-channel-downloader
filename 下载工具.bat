@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d %~dp0
"..\tg-channel-reposter\runtime\python\python.exe" downloader.py
pause