@echo off
REM Webhooks + approval page + payment callback on http://localhost:8000
cd /d "%~dp0"
set "PY=C:\Users\sanka\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m uvicorn src.payments.webhooks:app --port 8000
