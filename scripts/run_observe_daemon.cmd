@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run_observe_daemon.ps1" -Config "%SCRIPT_DIR%..\config\observe_daemon.example.json" -Interval 60
