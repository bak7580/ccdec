@echo off
rem Starts the ccdec window. Use this if double-clicking ccdec-gui.pyw does nothing:
rem the console stays open and shows the error.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m ccdec.gui %*
) else (
    python -m ccdec.gui %*
)
if errorlevel 1 (
    echo.
    echo Python 3.8 or newer is required: https://www.python.org/downloads/
    pause
)
