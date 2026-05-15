@echo off
REM Dev launcher — runs Cast Booster from the local source tree without packaging.
REM Assumes a venv has been created at app\.venv and requirements installed.

setlocal
cd /d "%~dp0.."
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m castbooster
) else (
    echo No .venv found. Run:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)
