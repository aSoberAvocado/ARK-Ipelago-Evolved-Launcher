@echo off
REM Build ARKipelago Launcher (--onedir --windowed). See build.py for details.
setlocal
cd /d "%~dp0"

REM The Python installer puts the py launcher on PATH but python.exe only when "Add
REM python.exe to PATH" was ticked, so a bare "python" can be missing. Prefer py.
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

%PY% -m pip install -r requirements-build.txt
if errorlevel 1 goto :error

%PY% build.py
if errorlevel 1 goto :error

echo.
echo Done. See "dist\ARKipelago Launcher\"
pause
goto :eof

:error
echo.
echo Build failed - see errors above.
pause
exit /b 1
