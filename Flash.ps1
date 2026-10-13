param([string]$Port)
$ErrorActionPreference='Stop'
if (-not $Port) {
  $ports=@([System.IO.Ports.SerialPort]::GetPortNames())
  if ($ports.Count -eq 1) {$Port=$ports[0]} else {
    Write-Host ('Available ports: '+($ports -join ', '))
    $Port=Read-Host 'PCB programming port (for example COM17)'
  }
}
if ($Port -notmatch '^COM[0-9]+$') {throw 'Enter a Windows COM port.'}
$release=Join-Path $PSScriptRoot 'release'
$manifest=Get-Content -LiteralPath (Join-Path $release 'manifest.json') -Raw | ConvertFrom-Json
foreach ($file in $manifest.files) {
  $path=Join-Path $release $file.name
  if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) {throw "Checksum mismatch: $($file.name)"}
}
$tool=Join-Path $release 'esptool.exe'
$backup=Join-Path $PSScriptRoot 'private-backups'
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$file=Join-Path $backup ((Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+$Port+'-nvs.bin')
& $tool --chip esp32s3 --port $Port read-flash 0x9000 0x5000 $file
if ($LASTEXITCODE -ne 0) {throw 'Calibration/config backup failed. Nothing flashed.'}
& $tool --chip esp32s3 --port $Port --baud 460800 write-flash --flash-mode keep --flash-freq keep --flash-size keep 0x0 (Join-Path $release 'bootloader.bin') 0x8000 (Join-Path $release 'partition-table.bin') 0xe000 (Join-Path $release 'ota_data_initial.bin') 0x10000 (Join-Path $release 'msai_sensing_node.bin')
if ($LASTEXITCODE -ne 0) {throw 'Flashing failed.'}
Write-Host 'Firmware installed. Calibration retained. The setup code is printed on the USB console at 115200 baud.'
Write-Host 'Run Open-Console.cmd, then press the PCB RESET button to see the setup code and network address.'
