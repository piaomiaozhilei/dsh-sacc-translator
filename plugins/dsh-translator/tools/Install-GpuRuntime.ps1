param([Parameter(Mandatory=$true)][string]$PluginPath)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PluginPath).Path
$manifest = Get-Content -LiteralPath (Join-Path $root 'package.json') -Raw | ConvertFrom-Json
if ($manifest.name -ne 'dsh-sacc-translator-cuda' -or $manifest.version -ne '__VERSION__') {
    throw 'Select the CUDA plugin directory for version __VERSION__.'
}
$archive = Join-Path $PSScriptRoot 'translator-gpu-runtime-__VERSION__.zip'
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '__SHA256__') {
    throw 'GPU component checksum mismatch. Download the matching archive again.'
}
$runtime = Join-Path $root 'runtime'
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'translator-worker.exe'))) {
    throw 'Translation runtime is missing.'
}
# This exact, checksum-verified archive contains only NVIDIA and ORT CUDA files.
Expand-Archive -LiteralPath $archive -DestinationPath $runtime -Force
Write-Output 'GPU component installed. New translation tasks can use CUDA.'
