@echo off
title JobHunter Dashboard (Public)
color 0B
echo.
echo  ============================================================
echo   JobHunter ^| Public Dashboard via ngrok
echo  ============================================================
echo.

:: ── Check pyngrok is installed ───────────────────────────────────────────────
py -c "import pyngrok" >nul 2>&1
if errorlevel 1 (
    echo  [INFO] pyngrok not installed — installing now...
    py -m pip install pyngrok --quiet
    if errorlevel 1 (
        echo  [ERROR] Failed to install pyngrok. Check your internet connection.
        pause & exit /b 1
    )
    echo  [OK] pyngrok installed
)

:: ── Check ngrok auth token ────────────────────────────────────────────────────
py -c "from pyngrok import conf; t=conf.get_default().auth_token; exit(0 if t else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [!] ngrok auth token not set.
    echo      1. Sign up free at https://ngrok.com
    echo      2. Copy your token from https://dashboard.ngrok.com/get-started/your-authtoken
    echo      3. Run:  ngrok config add-authtoken YOUR_TOKEN
    echo.
    echo  Proceeding anyway — ngrok may fail without a token.
    echo.
)

:: ── Check Python ──────────────────────────────────────────────────────────────
py --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    pause & exit /b 1
)

:: ── Make sure PostgreSQL is reachable ────────────────────────────────────────
echo  Checking PostgreSQL...
"C:\Program Files\PostgreSQL\16\bin\pg_isready.exe" -q 2>nul
if errorlevel 1 (
    echo  [WARN] PostgreSQL doesn't appear to be ready — proceeding anyway.
) else (
    echo  [OK] PostgreSQL ready
)

:: ── Start Redis if not already running ───────────────────────────────────────
echo  Checking Redis...
redis-cli ping >nul 2>&1
if errorlevel 1 (
    echo  [INFO] Starting Redis...
    start /B "Redis" redis-server
    timeout /t 2 /nobreak >nul
    echo  [OK] Redis started
) else (
    echo  [OK] Redis already running
)

:: ── Launch dashboard with public tunnel ──────────────────────────────────────
echo.
echo  Starting public tunnel...
echo  Public URL will be printed below once ngrok connects.
echo  Press Ctrl+C to stop.
echo  ============================================================
echo.
py dashboard.py --public

echo.
echo  Dashboard stopped.
pause
