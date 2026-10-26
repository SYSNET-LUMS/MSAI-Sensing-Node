param([Parameter(Mandatory=$true)][string]$EsptoolPath, [string]$BuildDirectory = 'build')
$ErrorActionPreference='Stop'
$version=([regex]::Match((Get-Content -LiteralPath (Join-Path $PSScriptRoot 'CMakeLists.txt') -Raw),'set\(PROJECT_VER "([^"]+)"\)')).Groups[1].Value
if (-not $version) {throw 'Project version is missing.'}
$release=Join-Path $PSScriptRoot 'release'
New-Item -ItemType Directory -Path $release -Force | Out-Null
$images=@{
  'bootloader.bin'=(Join-Path $BuildDirectory 'bootloader\bootloader.bin')
  'partition-table.bin'=(Join-Path $BuildDirectory 'partition_table\partition-table.bin')
  'ota_data_initial.bin'=(Join-Path $BuildDirectory 'ota_data_initial.bin')
  'msai_sensing_node.bin'=(Join-Path $BuildDirectory 'msai_sensing_node.bin')
}
foreach ($name in $images.Keys) {Copy-Item -LiteralPath (Join-Path $PSScriptRoot $images[$name]) -Destination (Join-Path $release $name)}
Copy-Item -LiteralPath $EsptoolPath -Destination (Join-Path $release 'esptool.exe')
$license=Join-Path (Split-Path -Parent $EsptoolPath) 'LICENSE'
if (-not (Test-Path -LiteralPath $license)) {throw 'Place the upstream esptool LICENSE alongside esptool.exe.'}
Copy-Item -LiteralPath $license -Destination (Join-Path $release 'LICENSE-esptool.txt')
$files=@('bootloader.bin','partition-table.bin','ota_data_initial.bin','msai_sensing_node.bin','esptool.exe') | ForEach-Object {
  $path=Join-Path $release $_
  @{name=$_;sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant();bytes=(Get-Item -LiteralPath $path).Length}
}
$sourceFiles=@(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'main') -File -Recurse)
$sourceFiles+=@('CMakeLists.txt','sdkconfig.defaults','partitions.csv','dependencies.lock','sdkconfig') | ForEach-Object {Get-Item -LiteralPath (Join-Path $PSScriptRoot $_)}
$sources=@($sourceFiles | ForEach-Object {@{name=$_.FullName.Substring($PSScriptRoot.Length+1).Replace('\','/');sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}})
@{version=$version;target='esp32s3';idf='5.5.5';hardware='MCSA R4';status='Bench build; see README.md in the package root';built_utc=(Get-Date).ToUniversalTime().ToString('o');files=@($files);sources=$sources} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $release 'manifest.json')
Write-Host "Packaged firmware in $release"


