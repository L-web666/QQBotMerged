@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  QQ Bot Merged - Windows launcher
REM  Auto restart on crash. Stop after 5 consecutive short runs.
REM ============================================================
cd /d "%~dp0"

set FAIL_COUNT=0
set MAX_FAILS=5
set MIN_UPTIME=60

:loop
echo.
echo ============================================================
echo [%date% %time%] Starting QQ Bot Merged ...
echo ============================================================

call :getsec START_SEC

python run.py

call :getsec END_SEC
set /a UPTIME=%END_SEC% - %START_SEC%
if %UPTIME% lss 0 set /a UPTIME=%UPTIME% + 86400

if %UPTIME% geq %MIN_UPTIME% (
  set FAIL_COUNT=0
  echo [INFO] Ran for %UPTIME% seconds. Failure counter reset.
) else (
  set /a FAIL_COUNT+=1
  echo [WARN] Ran for only %UPTIME% seconds. Failure count: !FAIL_COUNT! of %MAX_FAILS%
)

if !FAIL_COUNT! geq %MAX_FAILS% (
  echo.
  echo [ERROR] Failed %MAX_FAILS% times in a row. Stopping.
  echo Check the log above to find the cause.
  pause
  exit /b 1
)

echo [INFO] Restarting in 3 seconds. Press Ctrl+C twice to stop.
timeout /t 3 /nobreak >nul
goto loop

:getsec
setlocal
for /f "tokens=1-4 delims=:.," %%a in ("%time: =0%") do (
  set /a SEC=100%%a %% 100 * 3600 + 100%%b %% 100 * 60 + 100%%c %% 100
)
endlocal & set %1=%SEC%
goto :eof