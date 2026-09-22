@echo off
REM Starts the commerce MCP server for Claude Desktop. Errors go to mcp-error.log (stdout is the MCP channel).
cd /d "%~dp0"
set "PY=C:\Users\sanka\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m src.commerce_mcp.server 2> mcp-error.log
