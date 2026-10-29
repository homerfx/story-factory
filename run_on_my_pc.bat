@echo off
REM Runs the Story Factory on this Windows PC (close the window to stop it).
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "PY=python"
where python >nul 2>nul || set "PY=py"
%PY% --version >nul 2>nul || goto nopython

echo Checking the Python packages the factory needs...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt || goto pipfail

if not exist ".env" goto noenv

echo Starting the Story Factory. Close this window to stop it.
%PY% run.py --loop
pause
exit /b

:noenv
copy ".env.example" ".env" >nul
echo.
echo Your keys file .env was missing, so it was just created and opened in Notepad.
echo Paste your Discord bot token, server ID and at least the Groq key after the = signs,
echo save it, close Notepad, then run this file again.
start "" notepad ".env"
pause
exit /b

:pipfail
echo.
echo Installing the packages failed. Check your internet connection and run this file again.
pause
exit /b

:nopython
echo.
echo Python was not found. Install it from python.org, tick "Add python.exe to PATH", then run this again.
pause
exit /b
