@echo off
rem Builds dist\Zapi\Zapi.exe and, if Inno Setup is installed, the installer dist\Zapi-Setup.exe.
rem
rem   build.bat                 app + installer
rem   build.bat test            run the unit tests first and stop if one fails
rem   build.bat portable        also build the single-file portable edition dist\Zapi-Portable.exe
rem   build.bat all             test + app + installer + portable
rem   (set NOPAUSE=1 to skip the "press a key" pause when it was started by double-click)
rem Needs: Python 3.12+ on PATH; Inno Setup 6 for the installer (optional). Code signing: see SIGN_CERT_SHA1 below.
setlocal
cd /d "%~dp0"
set DO_TEST=0
set DO_PORTABLE=0
set RC=0
for %%A in (%*) do (
  if /i "%%A"=="test" set DO_TEST=1
  if /i "%%A"=="portable" set DO_PORTABLE=1
  if /i "%%A"=="all" (set DO_TEST=1& set DO_PORTABLE=1)
  if /i "%%A"=="help" goto :usage
  if /i "%%A"=="/?" goto :usage
)
python --version >nul 2>&1 || (echo Python was not found on PATH. Install Python 3.12 or newer and re-run. & goto :err)
python -m pip install --quiet -r requirements-dev.txt || goto :err
if "%DO_TEST%"=="1" (
  echo Running the unit tests...
  set QT_QPA_PLATFORM=offscreen
  python -W error::SyntaxWarning -m unittest discover -s tests -t . || (echo Tests failed - not building. & goto :err)
  set QT_QPA_PLATFORM=
)
python tools\make_assets.py || goto :err
python tools\make_version.py || goto :err
python tools\make_notices.py || goto :err
python -m PyInstaller --noconfirm --clean Zapi.spec || goto :err
rem --- drop components the app never loads ---
set I=dist\Zapi\_internal
del /q "%I%\PySide6\opengl32sw.dll" "%I%\PySide6\Qt6Quick*.dll" "%I%\PySide6\Qt6Pdf*.dll" "%I%\PySide6\Qt6OpenGL*.dll" "%I%\PySide6\Qt6VirtualKeyboard*.dll" 2>nul
del /q "%I%\PySide6\plugins\imageformats\qpdf.dll" "%I%\PySide6\plugins\platforminputcontexts\qtvirtualkeyboardplugin.dll" "%I%\PySide6\plugins\generic\qtuiotouchplugin.dll" 2>nul
rmdir /s /q "%I%\PySide6\translations" 2>nul
rem --- the script engine needs only QtQml's JS engine; PyInstaller also drags in every QML module and WebEngine (~250 MB) ---
rmdir /s /q "%I%\PySide6\qml" 2>nul
rmdir /s /q "%I%\PySide6\resources" 2>nul
del /q "%I%\PySide6\Qt6WebEngine*.dll" "%I%\PySide6\Qt6Web*.dll" "%I%\PySide6\Qt63D*.dll" "%I%\PySide6\Qt6Graphs*.dll" "%I%\PySide6\Qt6Charts*.dll" "%I%\PySide6\Qt6Location*.dll" "%I%\PySide6\Qt6Positioning*.dll" "%I%\PySide6\Qt6Sensors*.dll" "%I%\PySide6\Qt6Bluetooth*.dll" "%I%\PySide6\Qt6Nfc*.dll" "%I%\PySide6\Qt6RemoteObjects*.dll" "%I%\PySide6\Qt6Scxml*.dll" "%I%\PySide6\Qt6SerialPort*.dll" "%I%\PySide6\Qt6SpatialAudio*.dll" "%I%\PySide6\Qt6TextToSpeech*.dll" "%I%\PySide6\Qt6Multimedia*.dll" "%I%\PySide6\Qt6DataVisualization*.dll" "%I%\PySide6\Qt6ShaderTools*.dll" "%I%\PySide6\Qt6Sql*.dll" "%I%\PySide6\Qt6DBus*.dll" "%I%\PySide6\Qt6Designer*.dll" "%I%\PySide6\Qt6Help*.dll" 2>nul
del /q "%I%\PySide6\QtWebEngineProcess.exe" 2>nul
rem --- more trimming; the selftest below fails the build if something needed was removed ---
rmdir /s /q "%I%\PySide6\plugins\qmltooling" 2>nul
rmdir /s /q "%I%\PySide6\plugins\tls" 2>nul
del /q "%I%\PySide6\plugins\imageformats\qicns.dll" "%I%\PySide6\plugins\imageformats\qtga.dll" "%I%\PySide6\plugins\imageformats\qtiff.dll" "%I%\PySide6\plugins\imageformats\qwbmp.dll" 2>nul
del /q "%I%\PySide6\plugins\platforms\qdirect2d.dll" "%I%\PySide6\plugins\platforms\qminimal.dll" 2>nul
del /q "%I%\PySide6\Qt6LabsStyleKit*.dll" "%I%\PySide6\Qt6Test*.dll" "%I%\PySide6\Qt6StateMachine*.dll" 2>nul
rem Qt's TLS plugin (removed above) is the only user of this second OpenSSL copy; Python uses libssl-3.dll / libcrypto-3.dll
del /q "%I%\libssl-3-x64.dll" "%I%\libcrypto-3-x64.dll" 2>nul
rem the Universal C Runtime is part of Windows 10 and later
del /q "%I%\api-ms-win-*.dll" "%I%\ucrtbase.dll" 2>nul
dist\Zapi\Zapi.exe --selftest || (type "%TEMP%\zapi-selftest.log" & goto :err)
type "%TEMP%\zapi-selftest.log"
dist\Zapi\zapi-cli.exe --version || goto :err
echo.
echo Built: dist\Zapi\Zapi.exe

set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
rem --- code signing: removes the "Unknown publisher" warning ---
rem Set SIGN_CERT_SHA1 to the thumbprint of a code-signing certificate that is in your Windows certificate store
rem (optional SIGN_TIMESTAMP_URL), or set LAVATI_SIGNTOOL to a full command using $f for the file.
if defined SIGN_CERT_SHA1 call :findsigntool
if defined SIGN_CERT_SHA1 if defined SIGNTOOL_EXE call :makesigncmd
if defined LAVATI_SIGNTOOL call :signapp || goto :err
if defined LAVATI_SIGNTOOL call set ISIGN=%%LAVATI_SIGNTOOL:"=$q%%
if defined LAVATI_SIGNTOOL (echo Signing enabled) else (echo Signing skipped: SIGN_CERT_SHA1 is not set - the installer will show Unknown publisher)
if defined ISCC (
  call :runiscc || goto :err
  echo Installer: dist\Zapi-Setup.exe
) else (
  echo Inno Setup not found - skipping installer. Install it from https://jrsoftware.org/isdl.php and re-run.
)
if "%DO_PORTABLE%"=="1" (
  echo.
  echo Building the portable edition...
  call "%~dp0make_portable.bat" || goto :err
)
echo.
echo ===== Build finished =====
for %%F in (dist\Zapi-Setup.exe dist\Zapi-Portable.exe) do if exist "%%F" (
  echo %%F
  certutil -hashfile "%%F" SHA256 | findstr /r "^[0-9a-f][0-9a-f]*$"
)
goto :end

:usage
echo Usage: build.bat [test] [portable] [all]
echo   test      run the unit tests first
echo   portable  also build dist\Zapi-Portable.exe (one file, no installer)
echo   all       test + installer + portable
set RC=0
goto :end

:err
echo Build failed.
set RC=1

:end
rem pause only when started by double-click (Explorer runs "cmd /c"), so the window does not vanish
if not defined NOPAUSE echo %cmdcmdline% | find /i "/c" >nul && pause
exit /b %RC%

:runiscc
rem Inno Setup occasionally fails because antivirus or Explorer briefly locks the output file: try up to 3 times
set TRY=0
:iscctry
set /a TRY+=1
if defined LAVATI_SIGNTOOL ("%ISCC%" /DSign=1 "/Slavatisign=%ISIGN%" installer.iss) else ("%ISCC%" installer.iss)
if not errorlevel 1 exit /b 0
if %TRY% GEQ 3 exit /b 1
echo Inno Setup failed - retrying in 5 seconds...
timeout /t 5 /nobreak >nul
goto :iscctry

:findsigntool
set SIGNTOOL_EXE=
for /f "delims=" %%S in ('dir /b /s "%ProgramFiles(x86)%\Windows Kits\10\bin\signtool.exe" 2^>nul ^| findstr /i "\\x64\\"') do set SIGNTOOL_EXE=%%S
exit /b 0

:makesigncmd
if not defined SIGN_TIMESTAMP_URL set SIGN_TIMESTAMP_URL=http://timestamp.digicert.com
set LAVATI_SIGNTOOL="%SIGNTOOL_EXE%" sign /sha1 %SIGN_CERT_SHA1% /fd sha256 /tr %SIGN_TIMESTAMP_URL% /td sha256 /d "Zapi" $f
exit /b 0

:signapp
call set SIGNCMD=%%LAVATI_SIGNTOOL:$f=dist\Zapi\Zapi.exe%%
%SIGNCMD%
exit /b %errorlevel%
