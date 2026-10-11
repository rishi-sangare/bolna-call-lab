@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m uvicorn engine.server:app --host 127.0.0.1 --port 5001 --ws wsproto
