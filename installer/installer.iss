; Inno Setup script for Lavora, modelled on Zapi's installer.iss (reference\zapi-installer.iss.txt).
; Run installer\build.ps1 first (it builds dist\Lavora\ and passes /DAppVersion=...). Brand constants come from
; brand.iss.inc, which tools\make_brand.py generates from lavora\brand.py.
; Differences from Zapi: PrivilegesRequired=admin (raw disk access needs elevation), system-wide PATH entry, a fresh AppId,
; an optional "Rescue USB guide" task, and an uninstall prompt about the undo snapshots / audit log.
#include "brand.iss.inc"
; build.ps1 passes the real version; the other defines let CI build throw-away installers for upgrade / downgrade tests.
#ifndef AppVersion
  #define AppVersion DefaultVersion
#endif
#ifndef PayloadDir
  #define PayloadDir "..\dist\Lavora"
#endif
#ifndef OutName
  #define OutName "Lavora-Setup"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif

[Setup]
AppId={#AppIdSetup}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppSupportUrl}
AppCopyright={#AppCopyright}
LicenseFile=EULA.txt
#ifdef Sign
SignTool=lavatisign $f
SignedUninstaller=yes
#endif
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoVersion={#AppVersion}
WizardSizePercent=115
DisableProgramGroupPage=yes
AllowNoIcons=yes
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
SetupIconFile=..\assets\lavora.ico
OutputDir={#OutDir}
OutputBaseFilename={#OutName}
Compression=lzma2/ultra64
LZMAUseSeparateProcess=yes
SolidCompression=yes
WizardStyle=modern
WizardImageFile=wizard-large.bmp
WizardSmallImageFile=wizard-small.bmp
InfoBeforeFile=welcome.txt
DisableWelcomePage=no
DisableReadyPage=no
ShowLanguageDialog=no
SetupMutex=LavoraSetupMutex
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
DirExistsWarning=no
UsePreviousTasks=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
#ifdef NoAdmin
; ONLY for automated install tests where nobody can click a UAC prompt. The shipped installer is always admin.
PrivilegesRequired=lowest
#else
PrivilegesRequired=admin
#endif

[Messages]
WelcomeLabel1=Welcome to {#AppName}
WelcomeLabel2=This will install {#AppName} {#AppVersion} on your computer.%n%nIt is recommended that you close other applications before continuing.
FinishedHeadingLabel={#AppName} is ready
FinishedLabel=Setup has finished installing {#AppName}. Start it from the Start menu or the desktop shortcut. It will ask for administrator permission because it reads disks directly.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "addtopath"; Description: "Add the &lavora-cli command to the PATH"; GroupDescription: "Integration:"
Name: "rescueguide"; Description: "Show the Lavora &Rescue USB guide after installing (make a bootable repair USB)"; GroupDescription: "Rescue:"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\Rescue-USB-guide.txt"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\windows-boot-repair.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Rescue USB guide"; Filename: "{app}\docs\Rescue-USB-guide.txt"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
#ifdef NoAdmin
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))
#else
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))
#endif

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[InstallDelete]
; a clean update: remove files of the previous version that the new one no longer ships
; (the uninstaller, unins000.*, is left alone: it is replaced at the end of the install)
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\Lavora*.exe"
Type: files; Name: "{app}\lavora-*.exe"
Type: files; Name: "{app}\*.txt"

[Run]
Filename: "{app}\docs\Rescue-USB-guide.txt"; Description: "Rescue USB guide"; Flags: shellexec nowait postinstall skipifsilent; Tasks: rescueguide
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent runascurrentuser

[Code]
var
  KeepData: Boolean;

const
#ifdef NoAdmin
  EnvRoot = HKCU;
  EnvKey = 'Environment';
#else
  EnvRoot = HKLM;
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
#endif
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1';

function NeedsAddPath(Param: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then
    Paths := '';
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;

procedure RemoveFromPath(Param: string);
var
  Paths, Padded: string;
  P: Integer;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then Exit;
  { match whole entries only: C:\Lavora must not match C:\LavoraTools }
  Padded := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Param) + ';', Uppercase(Padded));
  if P = 0 then Exit;
  Delete(Padded, P, Length(Param) + 1);
  Paths := Copy(Padded, 2, Length(Padded) - 2);
  RegWriteExpandStringValue(EnvRoot, EnvKey, 'Path', Paths);
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

{ Stops every process that runs from the install folder (Lavora.exe, lavora-cli.exe) and nothing else. }
procedure StopProcessesIn(const Dir: String);
var
  Code: Integer;
  Folder, Params: String;
begin
  Folder := Dir;
  StringChangeEx(Folder, '''', '''''', True);
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$d = ''' + Folder + '\''; ' +
    'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) -and $_.Name -notlike ''unins*'' } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Exec(ExpandConstant('{cmd}'), '/c taskkill /F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(400);
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
  if not RegQueryStringValue(HKLM, UninstKey, 'DisplayVersion', Result) then
    if not RegQueryStringValue(HKCU, UninstKey, 'DisplayVersion', Result) then
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
      Result := HasParam('/ALLOWDOWNGRADE')                       { silent: refuse unless explicitly allowed }
    else
      Result := MsgBox('{#AppName} ' + Old + ' is already installed. It is newer than this setup (' + '{#AppVersion}' + ').' + #13#10 + #13#10 +
        'Install the older version anyway? Your settings are kept.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end;
end;

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
      DeleteTree(ExpandConstant('{localappdata}\{#AppName}'));
    { the uninstaller lives inside the install folder, so remove it a moment after it exits; rmdir without /s only removes
      the folder when it is empty, so a shared folder chosen at install time is never wiped. }
    Exec(ExpandConstant('{cmd}'), '/c ping 127.0.0.1 -n 3 >nul & del /q "' + ExpandConstant('{app}') + '\unins*.*" & rmdir "' + ExpandConstant('{app}') + '"', '', SW_HIDE, ewNoWait, Code);
  end;
end;

{ Undo snapshots (which let you reverse a repair), the audit log and the licence key are kept unless you say otherwise:
  interactive uninstall asks (default: keep); a silent uninstall keeps them unless started with /REMOVEDATA. }
function InitializeUninstall(): Boolean;
begin
  StopProcessesIn(ExpandConstant('{app}'));
  if UninstallSilent then
    KeepData := not HasParam('/REMOVEDATA')
  else
    KeepData := MsgBox('Keep your undo snapshots, audit log and licence key?' + #13#10 + #13#10 +
      'Yes (recommended) keeps them for a future install. Undo snapshots are the only way to reverse a repair you made. No removes all {#AppName} data from this computer.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON1) = IDYES;
  Result := True;
end;
