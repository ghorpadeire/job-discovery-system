@echo off
title JobScout v2 — Job Discovery System
color 0A
cls

echo.
echo  ============================================================
echo   JobScout v2.0 — Irish Job Discovery ^& Ghost Filter
echo  ============================================================
echo.

cd /d "%~dp0"

:: ── Check PostgreSQL is running ─────────────────────────────
echo  [1/5] Checking database...
py -c "from core.database import check_connection; import sys; sys.exit(0 if check_connection() else 1)" 2>nul
if errorlevel 1 (
    echo         Not running. Trying to start PostgreSQL service...
    net start postgresql-x64-16 >nul 2>&1
    timeout /t 3 /nobreak >nul
    py -c "from core.database import check_connection; import sys; sys.exit(0 if check_connection() else 1)" 2>nul
    if errorlevel 1 (
        color 0C
        echo.
        echo  ERROR: Cannot connect to PostgreSQL.
        echo  Go to: Windows Search → Services → find postgresql-x64-16 → Start
        echo  Then double-click START.bat again.
        echo.
        pause
        exit /b 1
    )
)
echo         Database: OK
echo.

:: ── Fix DB schema ────────────────────────────────────────────
echo  [2/5] Checking database schema...
py fix_db.py >nul 2>&1
echo         Schema: OK
echo.

:: ── First scrape ─────────────────────────────────────────────
echo  [3/5] Running first job scrape + scoring...
echo         (Takes 3-5 minutes — scraping IrishJobs.ie + Indeed Ireland)
echo.
py run_all.py --score --no-career-check
echo.

:: ── Reset alerts so ALL scraped jobs get sent to Telegram ───
echo  [4/5] Preparing Telegram alerts...
py tg_notify.py --reset-alerts >nul 2>&1
echo         Alert queue reset — all jobs will be sent
echo.

:: ── Start background poller in a new window ─────────────────
echo  [5/5] Starting background poller (checks every 30 minutes)...
start "JobScout Poller — Do Not Close" cmd /k "title JobScout Poller ^& color 0B ^& echo. ^& echo  JobScout is running. New jobs will be sent to Telegram every 30 min. ^& echo  Do NOT close this window. ^& echo. ^& py poller.py"
timeout /t 3 /nobreak >nul

:: ── Open dashboard ───────────────────────────────────────────
echo.
echo  ============================================================
echo   Opening dashboard at: http://localhost:5000
echo   Telegram alerts:      running in background window
echo   Press CTRL+C to stop the dashboard
echo  ============================================================
echo.

start "" "http://localhost:5000"
timeout /t 2 /nobreak >nul
py dashboard.py
