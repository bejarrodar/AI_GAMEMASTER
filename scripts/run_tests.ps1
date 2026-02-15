param(
    [string]$VenvPath = ".venv"
)

$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Virtual environment not found. Run .\scripts\setup_local_test_env.ps1 first."
}

& $pythonExe -m ruff check src tests
& $pythonExe -m pytest -q
