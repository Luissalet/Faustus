@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-Faustus.ps1" %*
echo.
if errorlevel 1 (
  echo Faustus could not be stopped completely.
) else (
  echo Done. You can close this window.
)
pause
