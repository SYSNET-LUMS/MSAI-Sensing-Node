param([string]$IdfPath = $env:IDF_PATH, [string]$BuildDirectory = 'build')
$ErrorActionPreference='Stop'
if (-not $IdfPath -or -not (Test-Path -LiteralPath (Join-Path $IdfPath 'export.ps1'))) { throw 'Open an ESP-IDF 5.5.5 terminal, or pass -IdfPath pointing to that SDK.' }
. (Join-Path $IdfPath 'export.ps1')
Push-Location $PSScriptRoot
try {
  python (Join-Path $IdfPath 'tools\idf.py') -B $BuildDirectory -DIDF_TARGET=esp32s3 build
  if ($LASTEXITCODE -ne 0) { throw 'Firmware build failed.' }
} finally { Pop-Location }
