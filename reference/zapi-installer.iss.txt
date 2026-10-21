; Inno Setup script - run build.bat first (it produces dist\Zapi\).
; The installer carries Lavati Softwares branding (wizard artwork, publisher); the app itself uses Zapi's own icon.
#define AppName "Zapi"
#define AppExe "Zapi.exe"
#define AppGuid "{3C7E2B54-9A1D-4F0B-8E63-5D2A7C91B4E8}"
; build.bat passes the real version (/DAppVersion=...); the other defines let tools\make_test_installers.ps1 build
; throw-away "older" and "newer" installers for the upgrade / downgrade tests
#ifndef AppVersion
  #define AppVersion "0.1"
#endif
#ifndef PayloadDir
  #define PayloadDir "dist\Zapi"
#endif
#ifndef OutName
  #define OutName "Zapi-Setup"
#endif
#ifndef OutDir
  #define OutDir "dist"
#endif

[Setup]
AppId={{3C7E2B54-9A1D-4F0B-8E63-5D2A7C91B4E8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Lavati Softwares
AppCopyright=Copyright (c) 2026 Lavati Softwares (MIT License)
LicenseFile=installer\EULA.txt
#ifdef Sign
SignTool=lavatisign $f
SignedUninstaller=yes
#endif
VersionInfoCompany=Lavati Softwares
VersionInfoDescription=Zapi Setup
VersionInfoProductName=Zapi
WizardSizePercent=115
DisableProgramGroupPage=yes
AllowNoIcons=yes
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=assets\zapi.ico
OutputDir={#OutDir}
OutputBaseFilename={#OutName}
Compression=lzma2/ultra64
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
SolidCompression=yes
WizardStyle=modern
WizardImageFile=installer\wizard-large.bmp
WizardSmallImageFile=installer\wizard-small.bmp
InfoBeforeFile=installer\welcome.txt
DisableWelcomePage=no
DisableReadyPage=no
ShowLanguageDialog=no
SetupMutex=ZapiSetupMutex
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
DirExistsWarning=no
UsePreviousTasks=yes
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes

[Messages]
WelcomeLabel1=Welcome to Zapi
WelcomeLabel2=This will install Zapi {#AppVersion} on your computer.%n%nIt is recommended that you close other applications before continuing.
FinishedHeadingLabel=Zapi is ready
FinishedLabel=Setup has finished installing Zapi. Start it from the Start menu or the desktop shortcut.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "addtopath"; Description: "Add the &zapi-cli command (collection runner for scripts and CI) to the PATH"; GroupDescription: "Integration:"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; per-user PATH entry for zapi-cli.exe (removed again on uninstall)
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[InstallDelete]
; a clean update: remove files of the previous version that the new one no longer ships
; (the uninstaller, unins000.*, is left alone: it is replaced at the end of the install)
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\Zapi*.exe"
Type: files; Name: "{app}\zapi-*.exe"
Type: files; Name: "{app}\*.txt"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  KeepData: Boolean;

const
  EnvKey = 'Environment';
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1';

function NeedsAddPath(Param: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then
    Paths := '';
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;

procedure RemoveFromPath(Param: string);
var
  Paths, Padded: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then Exit;
  { match whole entries only: C:\Zapi must not match C:\ZapiTools. The outer ';' make the first and last entry look like the others }
  Padded := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Param) + ';', Uppercase(Padded));
  if P = 0 then Exit;
  Delete(Padded, P, Length(Param) + 1);                    { ";entry" goes, the ';' after it stays as the separator }
  Paths := Copy(Padded, 2, Length(Padded) - 2);
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths);
end;

procedure DeleteTree(const Path: String);
begin
  if DirExists(Path) then DelTree(Path, True, True, True);
end;

function HasParam(const Name: String): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), Name) = 0 then Result := True;
end;

{ Stops every process that runs from the install folder (Zapi.exe, its script-engine helper, zapi-cli.exe) and nothing else:
  other copies of Zapi elsewhere on the computer are left alone. }
procedure StopProcessesIn(const Dir: String);
var
  Code: Integer;
  Folder, Params: String;
begin
  Folder := Dir;
  StringChangeEx(Folder, '''', '''''', True);                      { a ' in the path must be doubled inside a PowerShell string }
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$d = ''' + Folder + '\''; ' +
    'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) -and $_.Name -notlike ''unins*'' } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Exec(ExpandConstant('{cmd}'), '/c taskkill /F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(400);                                                     { let Windows release the file locks }
end;

{ --- versions: refuse to replace a newer install with an older one by accident --- }
function VersionPart(var S: String): Integer;
var
  P: Integer;
begin
  P := Pos('.', S);
  if P = 0 then
  begin
    Result := StrToIntDef(S, 0);
    S := '';
  end else
  begin
    Result := StrToIntDef(Copy(S, 1, P - 1), 0);
    S := Copy(S, P + 1, Length(S));
  end;
end;

function CompareVersions(A, B: String): Integer;
var
  i, X, Y: Integer;
begin
  Result := 0;
  for i := 1 to 4 do
  begin
    X := VersionPart(A);
    Y := VersionPart(B);
    if X <> Y then
    begin
      if X > Y then Result := 1 else Result := -1;
      Exit;
    end;
  end;
end;

function InstalledVersion(): String;
begin
  Result := '';
  if not RegQueryStringValue(HKCU, UninstKey, 'DisplayVersion', Result) then
    if not RegQueryStringValue(HKLM, UninstKey, 'DisplayVersion', Result) then
      Result := '';
end;

function InitializeSetup(): Boolean;
var
  Old: String;
begin
  Result := True;
  Old := InstalledVersion();
  if (Old <> '') and (CompareVersions(Old, '{#AppVersion}') > 0) then
  begin
    if WizardSilent then
      Result := HasParam('/ALLOWDOWNGRADE')                       { silent: refuse unless explicitly allowed (exit code 6) }
    else
      Result := MsgBox('Zapi ' + Old + ' is already installed. It is newer than this setup (' + '{#AppVersion}' + ').' + #13#10 + #13#10 +
        'Install the older version anyway? Your collections and settings are kept.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end;
end;

{ Windows asks a running Zapi to close first (CloseApplications); this removes whatever is still running from the
  folder, such as the script-engine helper, so the files can be replaced. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if DirExists(ExpandConstant('{app}')) then
    StopProcessesIn(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromPath(ExpandConstant('{app}'));
    if not KeepData then
      DeleteTree(ExpandConstant('{userappdata}\Zapi'));
    { the uninstaller lives inside the install folder, so remove it a moment after it exits.
      Only our own files go, and rmdir without /s only removes the folder when it is empty, so a
      shared folder chosen at install time is never wiped. }
    Exec(ExpandConstant('{cmd}'), '/c ping 127.0.0.1 -n 3 >nul & del /q "' + ExpandConstant('{app}') + '\unins*.*" & rmdir "' + ExpandConstant('{app}') + '"', '', SW_HIDE, ewNoWait, Code);
  end;
end;

{ Your collections, environments and history are kept unless you say otherwise:
  interactive uninstall asks (default: keep); a silent uninstall keeps them unless started with /REMOVEDATA. }
function InitializeUninstall(): Boolean;
begin
  StopProcessesIn(ExpandConstant('{app}'));                         { a running copy keeps its files locked }
  if UninstallSilent then
    KeepData := not HasParam('/REMOVEDATA')
  else
    KeepData := MsgBox('Keep your collections, environments and history?' + #13#10 + #13#10 +
      'Yes (recommended) keeps them for a future install. No removes all Zapi data from this computer, including saved secrets.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON1) = IDYES;
  Result := True;
end;
