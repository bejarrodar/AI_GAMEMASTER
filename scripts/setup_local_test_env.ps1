param(
    [string]$VenvPath = ".venv"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $VenvPath)) {
    python -m venv $VenvPath
}

$pythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found in virtual environment: $pythonExe"
}

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install --upgrade "setuptools>=78.1.1" wheel
& $pythonExe -m pip install -e ".[dev,ui]"

Write-Host "Local test environment ready."
Write-Host "Activate with: `"$VenvPath\Scripts\Activate.ps1`""
Write-Host "Run tests with: .\scripts\run_tests.ps1"
