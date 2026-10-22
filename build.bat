@echo off
rem Builds Lavora: tests, dist\Lavora\ (PyInstaller), self-test, dist\Lavora-Setup.exe and the portable ZIP.
rem This is a thin wrapper around installer\build.ps1; all options are passed through.
rem
rem   build.bat                  tests + app + installer + portable zip
rem   build.bat -Bump patch      raise the version first (patch | minor | major)
rem   build.bat -SkipTests       skip ruff / mypy / pytest
rem   build.bat -NoInstaller     app + portable zip only
rem   build.bat -TestInstaller   also build Lavora-Setup-NoAdmin.exe (for silent install tests)
rem (set NOPAUSE=1 to skip the "press a key" pause when it was started by double-click)
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\build.ps1" %*
set RC=%errorlevel%
if %RC%==0 (echo. & echo Build finished. Output is in dist\) else (echo. & echo Build failed.)
rem pause only when started by double-click (Explorer runs "cmd /c"), so the window does not vanish
if not defined NOPAUSE echo %cmdcmdline% | find /i "/c" >nul && pause
exit /b %RC%
