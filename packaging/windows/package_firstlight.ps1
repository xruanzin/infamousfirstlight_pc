param(
    [string]$Python = "python",
    [string]$OutputRoot = ""
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

& $Python -m pip install --disable-pip-version-check -q pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }

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
foreach ($Module in $Hidden) {
    $PyArgs += "--hidden-import"
    $PyArgs += $Module
}
$PyArgs += (Join-Path $Root "run_firstlight.py")

& $Python @PyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Remove-Item $OutputRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $OutputRoot | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "bin") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "scripts") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "tools") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "patches") | Out-Null

Copy-Item (Join-Path $PyDist "FirstLightBootstrap\*") $OutputRoot -Recurse -Force

Copy-Item (Join-Path $Root "out\bb-probe.exe") (Join-Path $OutputRoot "bin\") -Force
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
Copy-Item (Join-Path $Root "tools\legacy-cpu-plan.json") (Join-Path $OutputRoot "tools\") -Force
if (Test-Path (Join-Path $Root "patches\redzone.json")) {
    Copy-Item (Join-Path $Root "patches\redzone.json") (Join-Path $OutputRoot "patches\") -Force
}
Copy-Item (Join-Path $Root "LICENSE") $OutputRoot -Force
Copy-Item (Join-Path $Root "docs\FIRSTLIGHT_BOOTSTRAP.md") $OutputRoot -Force

$Readme = @'
inFAMOUS First Light native bootstrap - CUSA00575 v1.00

1. Keep your legally obtained decrypted game dump outside this folder.
2. Run:
   FirstLightBootstrap.exe "D:\Games\CUSA00575"
3. The bootstrap prepares the native image and starts the original x86-64 eboot.
4. Send user\last_run.log when the game stops or crashes.

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
