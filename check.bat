@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ASCII-only on purpose: cmd reads .bat in the OEM codepage,
rem so non-ASCII here would be garbled. All Chinese output comes from Python.

set PYEXE=C:\Program Files\Python312\python.exe
if not exist "%PYEXE%" set PYEXE=python

"%PYEXE%" tools\alive.py
echo.
pause
