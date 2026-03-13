@echo off
title JobScout — Send ALL Jobs to Telegram Now
color 0B
cls

echo.
echo  ============================================================
echo   FLOOD TELEGRAM — Send ALL existing jobs to Telegram now
echo   Use this to get a full dump of every job in the database
echo  ============================================================
echo.

cd /d "%~dp0"

echo  Step 1: Resetting alert flags on all jobs...
py tg_notify.py --reset-alerts
echo.

echo  Step 2: Sending ALL unalerted jobs to Telegram...
echo  (You will receive up to 15 messages per batch)
echo.
py tg_notify.py --alerts
echo.

echo  Done! Check your Telegram.
echo  Run this file again to send any remaining jobs.
echo.
pause
