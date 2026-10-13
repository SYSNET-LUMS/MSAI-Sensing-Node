$ErrorActionPreference='Stop'
Push-Location $PSScriptRoot
try {
  if (-not (Get-Command py -ErrorAction SilentlyContinue)) {throw 'Install Python 3.12 from python.org first, then run this file again.'}
  if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) {throw 'Could not create the Python 3.12 environment.'}
  }
  & .venv\Scripts\python.exe -m pip install -r requirements.txt
  if ($LASTEXITCODE -ne 0) {throw 'Could not install receiver dependencies.'}
  if (-not (Test-Path -LiteralPath 'identity\connection.json')) {
    $receiverAddress=Read-Host 'Receiver IP or hostname reachable from the node'
    if (-not $receiverAddress) {throw 'Receiver address is required.'}
    & .venv\Scripts\python.exe receiver.py init --host $receiverAddress --port 5443
    if ($LASTEXITCODE -ne 0) {throw 'Could not create the receiver identity.'}
  }
  Write-Host 'Import identity\connection.json into the node setup page, then run Start-Receiver.cmd.'
} finally {Pop-Location}
