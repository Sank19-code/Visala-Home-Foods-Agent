@echo off
REM Buyer agent (Claude). Usage: run_agent.bat "order 2 mango pickles under 600 rupees to 560001"
cd /d "%~dp0"
set "PY=C:\Users\sanka\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m src.buyer_agent.cli %*
