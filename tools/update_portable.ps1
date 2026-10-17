param(
    [Parameter(Mandatory=$true)]
    [string]$PortablePath
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ManifestPath = Join-Path $RepoRoot "runtime\update_manifest.json"

if (-not (Test-Path $ManifestPath)) {
    throw "Manifesto nao encontrado: $ManifestPath"
}
if (-not (Test-Path $PortablePath)) {
    throw "Pasta portatil nao encontrada: $PortablePath"
}

$manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
if ($manifest.schema -ne 1) {
    throw "Schema de manifesto incompativel: $($manifest.schema)"
}

$changed = @()
$unchanged = @()
$skipped = @()

foreach ($file in $manifest.files) {
    $rel = [string]$file.path
    if ([string]::IsNullOrWhiteSpace($rel)) { continue }

    $destination = Join-Path $PortablePath ($rel -replace '/', '\')
    $folder = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $folder | Out-Null

    $sourceInRepo = Join-Path $RepoRoot ($rel -replace '/', '\')
    if (Test-Path $sourceInRepo -PathType Leaf) {
        $temp = "$destination.new"
        Copy-Item -Force $sourceInRepo $temp
    }
    else {
        $url = [string]$file.url
        if ([string]::IsNullOrWhiteSpace($url)) {
            if ($file.required) { throw "URL ausente para $rel" }
            $skipped += $rel
            continue
        }
        $separator = if ($url.Contains("?")) { "&" } else { "?" }
        $freshUrl = "$url" + $separator + "nocache=" + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $temp = "$destination.new"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $freshUrl -OutFile $temp -Headers @{
                "Cache-Control" = "no-cache, no-store, max-age=0"
                "Pragma" = "no-cache"
            }
        }
        catch {
            if (-not $file.required) {
                Remove-Item -Force -ErrorAction SilentlyContinue $temp
                $skipped += $rel
                continue
            }
            throw
        }
    }

    $newSha = (Get-FileHash $temp -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($file.sha256) {
        $expected = ([string]$file.sha256).ToLowerInvariant()
        if ($newSha -ne $expected) {
            Remove-Item -Force -ErrorAction SilentlyContinue $temp
            throw "SHA-256 invalido para $rel (esperado=$expected recebido=$newSha)"
        }
    }

    $oldSha = $null
    if (Test-Path $destination -PathType Leaf) {
        $oldSha = (Get-FileHash $destination -Algorithm SHA256).Hash.ToLowerInvariant()
    }

    if ($oldSha -eq $newSha) {
        Remove-Item -Force -ErrorAction SilentlyContinue $temp
        $unchanged += $rel
        Write-Host "[OK] $rel"
        continue
    }

    Move-Item -Force $temp $destination
    $installedSha = (Get-FileHash $destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($installedSha -ne $newSha) {
        throw "Falha ao verificar arquivo instalado: $rel"
    }
    $changed += $rel
    Write-Host "[UPDATED] $rel  $installedSha"
}

Write-Host ""
Write-Host "Atualizacao concluida."
Write-Host "Alterados: $($changed.Count)"
Write-Host "Inalterados: $($unchanged.Count)"
if ($skipped.Count) {
    Write-Host "Opcionais ignorados: $($skipped -join ', ')"
}
