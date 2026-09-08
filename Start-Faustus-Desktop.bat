@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Faustus.ps1" -Desktop %*
if errorlevel 1 pause
