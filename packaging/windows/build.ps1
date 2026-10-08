# Build Mia-Browser-Use-Setup.exe: the Windows installer for people who never open a
# terminal. Per-user (no administrator prompt), into %LOCALAPPDATA%\Mia.
#
# Run on Windows with uv and Inno Setup 6 installed (winget install astral-sh.uv JRSoftware.InnoSetup):
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#   -> build\Mia-Browser-Use-Setup-<version>.exe and build\Mia-Browser-Use-Setup.exe
#
# Same shape as the Mac and Linux packages: its own Python with Mia's packages, the
# app, the helper Chrome starts, and the registry keys that point Chrome at the helper
# and make it offer the store extension by itself.
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent (Split-Path -Parent $Here)
$Build = Join-Path $Repo "build\win"
$Stage = Join-Path $Build "stage"
$PyVersion = if ($env:GHOST_PYTHON_VERSION) { $env:GHOST_PYTHON_VERSION } else { "3.13" }
$Version = (Get-Content (Join-Path $Repo "extension\manifest.json") -Raw | ConvertFrom-Json).version
$env:Path = "$env:LOCALAPPDATA\Programs\uv;$env:USERPROFILE\.local\bin;$env:Path"

if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force (Join-Path $Stage "app") | Out-Null

Write-Host "-> Python $PyVersion (standalone)"
uv python install $PyVersion | Out-Null
$PyExe = (uv python find --system $PyVersion).Trim()  # --system: never the repo venv
$PySrc = Split-Path -Parent $PyExe
Copy-Item -Recurse $PySrc (Join-Path $Stage "python")
$Py = Join-Path $Stage "python\python.exe"
Remove-Item -Force (Join-Path $Stage "python\Lib\EXTERNALLY-MANAGED") -ErrorAction SilentlyContinue
uv pip install --quiet --python $Py -r (Join-Path $Repo "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "installing Mia's packages failed (disk space?)" }

Write-Host "-> Mia $Version"
Get-ChildItem (Join-Path $Repo "*.py") | Copy-Item -Destination (Join-Path $Stage "app")
Copy-Item -Recurse (Join-Path $Repo "extension") (Join-Path $Stage "app\extension")
Copy-Item -Recurse (Join-Path $Repo "mia_skills") (Join-Path $Stage "app\mia_skills")
& $Py -m compileall -q (Join-Path $Stage "app") | Out-Null

# The helper Chrome starts, and a command for people who do open a terminal.
# %~dp0 is the folder the .bat lives in, so the install folder can be anywhere.
@"
@echo off
"%~dp0python\python.exe" "%~dp0app\native_host.py" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Stage "native-host.bat")
@"
@echo off
"%~dp0python\python.exe" "%~dp0app\ghost_cli.py" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Stage "mia-browser-use.cmd")

# Stops a running helper (install of a newer version, uninstall). Chrome starts the new one.
@'
$app = Join-Path $PSScriptRoot "app"
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*$app\ghost_cli.py*" -or $_.CommandLine -like "*$app\native_host.py*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
'@ | Set-Content -Encoding ASCII (Join-Path $Stage "stop-helper.ps1")

Push-Location $Repo
$ExtId = (& $Py -c "from pathlib import Path; from native_host import extension_id; print(extension_id(Path('extension')))").Trim()
Pop-Location
if ($ExtId -notmatch '^[a-p]{32}$') { throw "bad extension id: $ExtId" }

# The host manifest: Chrome finds it through the registry (set by the installer).
@{
  name = "com.ghost.bridge"; description = "Mia helper pairing"; type = "stdio"
  path = "native-host.bat"  # relative to this file
  allowed_origins = @("chrome-extension://$ExtId/")
} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $Stage "com.ghost.bridge.json")

Write-Host "-> Installer"
$Iss = Get-Content (Join-Path $Here "mia.iss") -Raw
$Iss = $Iss.Replace("@VERSION@", $Version).Replace("@EXT_ID@", $ExtId).Replace("@STAGE@", $Stage).Replace("@OUT@", (Join-Path $Repo "build"))
$IssPath = Join-Path $Build "mia.iss"
Set-Content -Encoding UTF8 $IssPath $Iss
$Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $Iscc /Q $IssPath
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
Copy-Item (Join-Path $Repo "build\Mia-Browser-Use-Setup-$Version.exe") (Join-Path $Repo "build\Mia-Browser-Use-Setup.exe") -Force
$Size = [math]::Round((Get-Item (Join-Path $Repo "build\Mia-Browser-Use-Setup.exe")).Length / 1MB)
Write-Host "OK build\Mia-Browser-Use-Setup-$Version.exe (${Size}M), also build\Mia-Browser-Use-Setup.exe"
Write-Host "   extension id: $ExtId"
