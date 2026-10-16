param(
    [Parameter(Position=0)]
    [string]$GameDir = "",
    [switch]$Console,
    [switch]$PrepareOnly,
    [int]$Timeout = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    $Python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $Python) {
    throw "Python 3 was not found in PATH."
}

$argsList = @("run_firstlight.py")
if ($GameDir) { $argsList += $GameDir }
if ($Console) { $argsList += "--console" }
if ($PrepareOnly) { $argsList += "--prepare-only" }
if ($Timeout -gt 0) {
    $argsList += "--timeout"
    $argsList += "$Timeout"
}

& $Python.Source @argsList
exit $LASTEXITCODE
