@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
py -3 -c "import sys; sys.exit(sys.version_info < (3,9))" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; sys.exit(sys.version_info < (3,9))" >nul 2>&1
if not errorlevel 1 goto use_python
echo Python 3.9 or newer is required. Install Python, then run LinqDrop.bat again.
pause
exit /b 1
:use_py
py -3 "%~dp0launcher.py" %*
exit /b %ERRORLEVEL%
:use_python
python "%~dp0launcher.py" %*
exit /b %ERRORLEVEL%
