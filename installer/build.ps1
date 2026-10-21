<#
.SYNOPSIS
  Builds Lavora: tests, PyInstaller one-dir app, self-test, Inno Setup installer and portable ZIP into dist\.

.EXAMPLE
  installer\build.ps1                    # tests + app + installer + portable zip (current version)
  installer\build.ps1 -Bump patch        # 0.1.0 -> 0.1.1 first (writes lavora\__init__.py)
  installer\build.ps1 -SkipTests         # skip ruff / mypy / pytest
  installer\build.ps1 -NoInstaller       # app + portable zip only
  installer\build.ps1 -TestInstaller     # also build Lavora-Setup-NoAdmin.exe (silent-install tests without a UAC prompt)

  Needs: Python 3.12+ on PATH, Inno Setup 6 (https://jrsoftware.org/isdl.php). Signing: set SIGN_CERT_SHA1 to a code-signing
  certificate thumbprint in your Windows store (optional SIGN_TIMESTAMP_URL); see docs\code-signing.md.
#>
[CmdletBinding()]
param(
  [ValidateSet("none", "patch", "minor", "major")] [string]$Bump = "none",
  [switch]$SkipTests,
  [switch]$NoInstaller,
  [switch]$TestInstaller
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail([string]$msg) { Write-Host "BUILD FAILED: $msg" -ForegroundColor Red; exit 1 }
function Step([string]$msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Run([string]$what, [scriptblock]$cmd) {
  & $cmd
  if ($LASTEXITCODE -ne 0) { Fail "$what (exit code $LASTEXITCODE)" }
}

Step "Checking tools"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail "Python was not found on PATH. Install Python 3.12 or newer and re-run." }
$pyver = (python -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ([version]$pyver -lt [version]"3.12") { Fail "Python $pyver is too old; Lavora needs 3.12+." }

$iscc = $null
if (-not $NoInstaller) {
  foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
    if ($c -and (Test-Path $c)) { $iscc = $c; break }
  }
  if (-not $iscc) { Fail "Inno Setup 6 (ISCC.exe) was not found. Install it from https://jrsoftware.org/isdl.php, or pass -NoInstaller." }
}

Step "Installing build dependencies"
Run "pip install" { python -m pip install --quiet -r requirements-dev.txt }

if ($Bump -ne "none") {
  Step "Bumping version ($Bump)"
  $initPath = Join-Path $root "lavora\__init__.py"
  $text = Get-Content $initPath -Raw
  if ($text -notmatch '__version__ = "(\d+)\.(\d+)\.(\d+)"') { Fail "could not find __version__ in lavora\__init__.py" }
  $maj = [int]$Matches[1]; $min = [int]$Matches[2]; $pat = [int]$Matches[3]
  switch ($Bump) { "major" { $maj++; $min = 0; $pat = 0 } "minor" { $min++; $pat = 0 } "patch" { $pat++ } }
  $new = "$maj.$min.$pat"
  Set-Content -Path $initPath -Value ($text -replace '__version__ = "[^"]+"', "__version__ = `"$new`"") -NoNewline -Encoding utf8
  Write-Host "version is now $new"
}
$version = (python -c "from lavora import __version__; print(__version__)")
if ($LASTEXITCODE -ne 0 -or -not $version) { Fail "cannot read lavora.__version__" }
Write-Host "Building Lavora $version"

if (-not $SkipTests) {
  Step "Lint, type check, tests"
  Run "ruff" { python -m ruff check . }
  Run "mypy" { python -m mypy lavora }
  $env:QT_QPA_PLATFORM = "offscreen"
  Run "pytest" { python -m pytest }
  Remove-Item Env:QT_QPA_PLATFORM
}

Step "Generating assets, version resources, brand include, notices"
Run "make_assets" { python tools\make_assets.py }
Run "make_version" { python tools\make_version.py }
Run "make_brand" { python tools\make_brand.py }
Run "make_notices" { python tools\make_notices.py }

Step "PyInstaller"
if (Test-Path dist\Lavora) { Remove-Item dist\Lavora -Recurse -Force }
Run "PyInstaller" { python -m PyInstaller --noconfirm --clean Lavora.spec }
$i = "dist\Lavora\_internal"
# drop Qt components the widgets UI never loads; the self-test below fails the build if something needed was removed
foreach ($pat in @("PySide6\opengl32sw.dll", "PySide6\Qt6Quick*.dll", "PySide6\Qt6Pdf*.dll", "PySide6\Qt6OpenGL*.dll", "PySide6\Qt6VirtualKeyboard*.dll",
                   "PySide6\Qt6Qml*.dll", "PySide6\Qt6Web*.dll", "PySide6\Qt63D*.dll", "PySide6\Qt6Multimedia*.dll", "PySide6\Qt6Sql*.dll",
                   "PySide6\Qt6Designer*.dll", "PySide6\Qt6Help*.dll", "PySide6\Qt6Test*.dll", "PySide6\QtWebEngineProcess.exe",
                   "PySide6\plugins\imageformats\qpdf.dll", "PySide6\plugins\platforms\qdirect2d.dll", "PySide6\plugins\platforms\qminimal.dll",
                   "api-ms-win-*.dll", "ucrtbase.dll")) {
  Remove-Item (Join-Path $i $pat) -Force -ErrorAction SilentlyContinue
}
foreach ($d in @("PySide6\qml", "PySide6\resources", "PySide6\translations", "PySide6\plugins\qmltooling", "PySide6\plugins\tls")) {
  Remove-Item (Join-Path $i $d) -Recurse -Force -ErrorAction SilentlyContinue
}

Step "Self-test of the built program"
# lavora-cli.exe runs the selftest without a UAC prompt (the GUI exe requires administrator rights)
Run "selftest" { & "dist\Lavora\lavora-cli.exe" --selftest }
Run "cli version" { & "dist\Lavora\lavora-cli.exe" version }
Copy-Item docs\Rescue-USB-guide.txt, docs\windows-boot-repair.md -Destination dist\Lavora -Force

Step "Portable ZIP"
$zip = "dist\Lavora-$version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path dist\Lavora\* -DestinationPath $zip -CompressionLevel Optimal

if (-not $NoInstaller) {
  Step "Inno Setup"
  $isccArgs = @("/DAppVersion=$version")
  if ($env:SIGN_CERT_SHA1) {
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -match "\\x64\\" } | Select-Object -Last 1
    if (-not $signtool) { Fail "SIGN_CERT_SHA1 is set but signtool.exe (Windows SDK) was not found." }
    $ts = if ($env:SIGN_TIMESTAMP_URL) { $env:SIGN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    $signCmd = "`"$($signtool.FullName)`" sign /sha1 $($env:SIGN_CERT_SHA1) /fd sha256 /tr $ts /td sha256 /d Lavora `$f"
    Run "sign app" { & $signtool.FullName sign /sha1 $env:SIGN_CERT_SHA1 /fd sha256 /tr $ts /td sha256 /d Lavora "dist\Lavora\Lavora.exe" "dist\Lavora\lavora-cli.exe" }
    $isccArgs += @("/DSign=1", "/Slavatisign=$($signCmd -replace '"', '$q')")
    Write-Host "Signing enabled"
  } else {
    Write-Host "Signing skipped: SIGN_CERT_SHA1 is not set - Windows SmartScreen will warn about an unknown publisher (docs\code-signing.md)."
  }
  Run "ISCC" { & $iscc @isccArgs installer\installer.iss }
  if ($TestInstaller) {
    Run "ISCC (no-admin test installer)" { & $iscc @isccArgs "/DNoAdmin=1" "/DOutName=Lavora-Setup-NoAdmin" installer\installer.iss }
  }
}

Step "Done"
Get-ChildItem dist\Lavora-*.exe, dist\Lavora-*.zip -ErrorAction SilentlyContinue | ForEach-Object {
  $h = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
  "{0,-40} {1,8:N1} MB  SHA256 {2}" -f $_.Name, ($_.Length / 1MB), $h
}
