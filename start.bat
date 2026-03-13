@echo off
title JobHunter Dashboard
color 0A
echo.
echo  ============================================================
echo   JobHunter ^| Local Dashboard
echo  ============================================================
echo.

:: ── Check Python ─────────────────────────────────────────────────────────────
py --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Make sure py.exe is on your PATH.
    pause & exit /b 1
)

:: ── Make sure PostgreSQL is reachable ────────────────────────────────────────
echo  Checking PostgreSQL...
"C:\Program Files\PostgreSQL\16\bin\pg_isready.exe" -q 2>nul
if errorlevel 1 (
    echo  [WARN] PostgreSQL doesn't appear to be ready.
    echo         It may still start — proceeding anyway.
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

:: ── Open browser after a short delay ─────────────────────────────────────────
start /B "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:5000"

:: ── Launch dashboard ──────────────────────────────────────────────────────────
echo.
echo  Dashboard → http://127.0.0.1:5000
echo  Press Ctrl+C to stop.
echo  ============================================================
echo.
py dashboard.py

echo.
echo  Dashboard stopped.
pause
