param(
    [string]$Python = "python",
    [string]$OutputRoot = "",
    [string]$IconPath = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $Root "out\firstlight-portable"
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)

$Probe = Join-Path $Root "out\bb-probe.exe"
if (-not (Test-Path $Probe)) {
    throw "Build out\bb-probe.exe first."
}

& $Python -m pip install --disable-pip-version-check -q pyinstaller pillow
if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller/Pillow." }

# Keep copyrighted game artwork out of the repository. For a personal build, the packager can
# consume icon0.png directly from the user's own CUSA00575 dump (or an explicit -IconPath).
if (-not $IconPath -and $env:FL_ICON_PATH) {
    $IconPath = $env:FL_ICON_PATH
}
if (-not $IconPath -and $env:FL_GAME_DIR) {
    $Candidate = Join-Path $env:FL_GAME_DIR "sce_sys\icon0.png"
    if (Test-Path $Candidate) { $IconPath = $Candidate }
}
# CI does not have access to the user's game directory. Use the compact launcher-only derivative
# supplied for this project so the portable FirstLightBootstrap.exe still carries First Light art.
if (-not $IconPath) {
    $EmbeddedB64 = Join-Path $Root "packaging\windows\firstlight_icon.jpg.b64"
    if (Test-Path $EmbeddedB64) {
        $EmbeddedJpg = Join-Path $Root "out\firstlight-icon-source.jpg"
        $Bytes = [Convert]::FromBase64String((Get-Content $EmbeddedB64 -Raw).Trim())
        [IO.File]::WriteAllBytes($EmbeddedJpg, $Bytes)
        $IconPath = $EmbeddedJpg
    }
}
$IconIco = ""
if ($IconPath) {
    $IconPath = [IO.Path]::GetFullPath($IconPath)
    if (-not (Test-Path $IconPath)) { throw "First Light icon not found: $IconPath" }
    $IconIco = Join-Path $Root "out\firstlight-icon.ico"
    $IconScript = @'
from PIL import Image
import sys
im = Image.open(sys.argv[1]).convert("RGBA")
im.save(sys.argv[2], format="ICO", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
'@
    $IconScript | & $Python - $IconPath $IconIco
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $IconIco)) { throw "Failed to convert First Light icon." }
    Write-Host "Using First Light icon: $IconPath"
}

$PyDist = Join-Path $Root "out\firstlight-pyi-dist"
$PyWork = Join-Path $Root "out\firstlight-pyi-work"
Remove-Item $PyDist,$PyWork -Recurse -Force -ErrorAction SilentlyContinue

$Hidden = @(
    "argparse","base64","bisect","collections","contextlib","ctypes","hashlib","json",
    "re","runpy","shutil","struct","tempfile","time","pathlib",
    "prepare","link_libc","cpu_compat"
)
$PyArgs = @(
    "-m","PyInstaller",
    "--noconfirm","--clean","--log-level","WARN","--console",
    "--name","FirstLightBootstrap",
    "--distpath",$PyDist,
    "--workpath",$PyWork,
    "--specpath",$PyWork,
    "--paths",(Join-Path $Root "scripts")
)
if ($IconIco) {
    $PyArgs += "--icon"
    $PyArgs += $IconIco
}
foreach ($Module in $Hidden) {
    $PyArgs += "--hidden-import"
    $PyArgs += $Module
}
$PyArgs += (Join-Path $Root "run_firstlight.py")

& $Python @PyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

# Build a standalone windowed launcher so end users never need PowerShell.
$LauncherDist = Join-Path $Root "out\firstlight-launcher-dist"
$LauncherWork = Join-Path $Root "out\firstlight-launcher-work"
Remove-Item $LauncherDist,$LauncherWork -Recurse -Force -ErrorAction SilentlyContinue
$LauncherArgs = @(
    "-m","PyInstaller",
    "--noconfirm","--clean","--log-level","WARN",
    "--onefile","--windowed",
    "--name","FirstLightLauncher",
    "--distpath",$LauncherDist,
    "--workpath",$LauncherWork,
    "--specpath",$LauncherWork
)
if ($IconIco) {
    $LauncherArgs += "--icon"
    $LauncherArgs += $IconIco
}
$LauncherArgs += (Join-Path $Root "launcher_firstlight.py")
& $Python @LauncherArgs
if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $LauncherDist "FirstLightLauncher.exe"))) {
    throw "FirstLightLauncher build failed."
}

Remove-Item $OutputRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $OutputRoot | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "bin") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "scripts") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "tools") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "patches") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "src") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "runtime") | Out-Null

Copy-Item (Join-Path $PyDist "FirstLightBootstrap\*") $OutputRoot -Recurse -Force
Copy-Item (Join-Path $LauncherDist "FirstLightLauncher.exe") (Join-Path $OutputRoot "FirstLightLauncher.exe") -Force

Copy-Item (Join-Path $Root "out\bb-probe.exe") (Join-Path $OutputRoot "bin\") -Force
if (Test-Path (Join-Path $Root "out\firstlight_runtime.dll")) {
    Copy-Item (Join-Path $Root "out\firstlight_runtime.dll") (Join-Path $OutputRoot "runtime\firstlight_runtime.dll") -Force
}
Copy-Item (Join-Path $Root "runtime\import_aliases.tsv") (Join-Path $OutputRoot "runtime\import_aliases.tsv") -Force
Copy-Item (Join-Path $Root "runtime\firstlight_config.json") (Join-Path $OutputRoot "runtime\firstlight_config.json") -Force
Copy-Item (Join-Path $Root "runtime\update_manifest.json") (Join-Path $OutputRoot "runtime\update_manifest.json") -Force
Copy-Item (Join-Path $Root "out\bb-gpu-capabilities.exe") (Join-Path $OutputRoot "bin\") -Force
if (Test-Path (Join-Path $Root "out\bb-play.exe")) {
    Copy-Item (Join-Path $Root "out\bb-play.exe") (Join-Path $OutputRoot "bin\") -Force
}

$Scripts = @(
    "prepare.py","link_libc.py","link_modules.py","cpu_compat.py","content_profile.py"
)
foreach ($Name in $Scripts) {
    Copy-Item (Join-Path $Root "scripts\$Name") (Join-Path $OutputRoot "scripts\$Name") -Force
}
Copy-Item (Join-Path $Root "tools\firstlight_triage.py") (Join-Path $OutputRoot "tools\") -Force
Copy-Item (Join-Path $Root "tools\firstlight_coverage.py") (Join-Path $OutputRoot "tools\") -Force
Copy-Item (Join-Path $Root "tools\firstlight_modules.py") (Join-Path $OutputRoot "tools\") -Force
Copy-Item (Join-Path $Root "tools\firstlight_log.py") (Join-Path $OutputRoot "tools\") -Force
Copy-Item (Join-Path $Root "tools\legacy-cpu-plan.json") (Join-Path $OutputRoot "tools\") -Force
Copy-Item (Join-Path $Root "src\import_names.inc") (Join-Path $OutputRoot "src\") -Force
if (Test-Path (Join-Path $Root "patches\redzone.json")) {
    Copy-Item (Join-Path $Root "patches\redzone.json") (Join-Path $OutputRoot "patches\") -Force
}
Copy-Item (Join-Path $Root "LICENSE") $OutputRoot -Force
Copy-Item (Join-Path $Root "docs\FIRSTLIGHT_BOOTSTRAP.md") $OutputRoot -Force

$Readme = @'
inFAMOUS First Light native bootstrap - CUSA00575 v1.00 / v1.04

1. Keep your legally obtained decrypted game dump outside this folder.
2. Double-click FirstLightLauncher.exe.
3. Choose the extracted CUSA00575 game folder. The launcher remembers it.
4. Click "Iniciar jogo". The launcher shows the live log and explains why boot stopped.
5. Use "Copiar relatório" or "Salvar relatório..." and send that report when the game stops.

Advanced/manual mode:
  FirstLightBootstrap.exe "D:\Games\CUSA00575"

Required in the dump:
  eboot.bin
  sce_sys\param.sfo
  sce_module\libc.prx
  sce_module\libSceFios2.prx

The runtime accepts payloads either directly under the game folder or inside
dvdroot_ps4\ (for example art\, movies\, sound\, playgo.pgm).

No game files are included in this package.
'@
$Readme | Set-Content -Path (Join-Path $OutputRoot "README-FIRSTLIGHT.txt") -Encoding UTF8

Write-Host "Portable bootstrap staged at $OutputRoot"
