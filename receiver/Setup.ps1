$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath 'dist\index.html')) {
        throw 'The built dashboard is missing. Restore dist, or rebuild with npm ci and npm run build.'
    }
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        $Python = Get-Command py.exe -ErrorAction SilentlyContinue
        $PythonArgs = @('-3')
        if (-not $Python) {
            $Python = Get-Command python.exe -ErrorAction SilentlyContinue
            $PythonArgs = @()
        }
        if (-not $Python) { throw 'Install Python 3.12, 3.13, or 3.14, then run Setup.cmd again.' }
        & $Python.Source @PythonArgs -c "import sys; sys.exit(0 if (3,12) <= sys.version_info < (3,15) else 1)"
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12, 3.13, or 3.14 is required.' }
        & $Python.Source @PythonArgs -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
    }
    & '.\.venv\Scripts\python.exe' -c "import sys; print('Python:', sys.version); sys.exit(0 if (3,12) <= sys.version_info < (3,15) else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'The installed environment needs Python 3.12, 3.13, or 3.14.' }
    & '.\.venv\Scripts\python.exe' -m pip install -r backend\requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed. Check the error above and Internet access.' }
    Write-Host 'Setup complete. Run Start-Dashboard.cmd, then open http://127.0.0.1:8000.' -ForegroundColor Green
    Write-Host 'The built dashboard is included; Node.js is only needed when editing frontend source.'
} catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
