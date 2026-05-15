@echo off
REM Chrome Native Messaging host launcher for Cast Booster.
REM Chrome spawns this .bat (path referenced in the generated manifest JSON),
REM which in turn invokes the venv Python to run castbooster.nm_host.
REM Must NOT print anything to stdout — Chrome treats stdout as the wire.

cd /d "%~dp0.."
".venv\Scripts\python.exe" -m castbooster.nm_host
