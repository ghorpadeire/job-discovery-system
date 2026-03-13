@echo off
title JobScout v2 — First Time Setup
color 0A
cls

echo.
echo  ============================================================
echo   JobScout v2.0 — First Time Setup
echo   Run this ONCE before using START.bat
echo  ============================================================
echo.

:: ── Check Python ────────────────────────────────────────────
echo  [1/6] Checking Python...
py --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  ERROR: Python not found. Install Python 3.10+ from python.org
    echo  Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('py --version') do echo         %%i found
echo.

:: ── Check PostgreSQL ────────────────────────────────────────
echo  [2/6] Checking PostgreSQL...
"C:\Program Files\PostgreSQL\16\bin\psql.exe" --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  ERROR: PostgreSQL 16 not found at expected location.
    echo  Expected: C:\Program Files\PostgreSQL\16\bin\
    pause
    exit /b 1
)
echo         PostgreSQL 16 found
echo.

:: ── Install Python packages ─────────────────────────────────
echo  [3/6] Installing Python packages (this takes 2-3 minutes)...
py -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    color 0C
    echo  ERROR: pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo         Packages installed
echo.

:: ── Install Playwright browser ──────────────────────────────
echo  [4/6] Installing Chromium browser for scraping...
py -m playwright install chromium
if errorlevel 1 (
    color 0C
    echo  ERROR: Playwright install failed.
    pause
    exit /b 1
)
echo         Chromium installed
echo.

:: ── Create database ─────────────────────────────────────────
echo  [5/6] Setting up database...
"C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE USER jobsuser WITH PASSWORD 'jobspass';" 2>nul
"C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE DATABASE jobsdb OWNER jobsuser;" 2>nul
py fix_db.py
if errorlevel 1 (
    color 0E
    echo  WARNING: DB fix had issues. Check PostgreSQL is running.
    echo  Try running: net start postgresql-x64-16
)
echo.

:: ── Check .env file ─────────────────────────────────────────
echo  [6/6] Checking configuration...
findstr /C:"<your-bot-token-here>" .env >nul 2>&1
if not errorlevel 1 (
    color 0E
    echo.
    echo  ============================================================
    echo   ACTION REQUIRED: Fill in your Telegram credentials in .env
    echo  ============================================================
    echo.
    echo   1. Open .env in Notepad
    echo   2. Set TELEGRAM_BOT_TOKEN  (get from @BotFather on Telegram)
    echo   3. Set TELEGRAM_CHAT_ID    (get from @userinfobot on Telegram)
    echo.
    echo   The app works without Telegram, but you won't get alerts.
    echo.
    pause
)

color 0A
echo.
echo  ============================================================
echo   Setup complete! You can now double-click START.bat
echo  ============================================================
echo.
pause
