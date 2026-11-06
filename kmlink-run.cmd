@echo off
rem ---------------------------------------------------------------
rem  km_link_switch - switch keyboard+mouse to the other PC.
rem  Double-click this file, then press Alt+S to switch back
rem  (or Ctrl+Alt+F12 if the hook is ever unhappy).
rem ---------------------------------------------------------------
setlocal
set "HERE=%~dp0"
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%HERE%km_link_switch.py" run %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [!] km_link_switch exited with code %RC%
    pause
)
endlocal
