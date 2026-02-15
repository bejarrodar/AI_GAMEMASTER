param(
    [string]$VenvPath = ".venv",
    [string]$SqlitePath = ".\aigm_local.db",
    [string]$OllamaUrl = "http://127.0.0.1:11434",
    [string]$OllamaModel = "llama3.2:3b",
    [int]$StreamlitPort = 9531
)

$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Virtual environment not found. Run .\scripts\setup_local_test_env.ps1 first."
}

$parentDir = Split-Path -Path $SqlitePath -Parent
if ([string]::IsNullOrWhiteSpace($parentDir)) {
    $parentDir = "."
}
if (-not (Test-Path $parentDir)) {
    New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
}

$portFromEnvFile = $null
if (Test-Path ".env") {
    $portLine = Get-Content ".env" | Where-Object { $_ -match "^AIGM_STREAMLIT_PORT=" } | Select-Object -First 1
    if ($portLine) {
        $portRaw = $portLine.Substring("AIGM_STREAMLIT_PORT=".Length).Trim()
        if ($portRaw -match "^\d+$") {
            $portFromEnvFile = [int]$portRaw
        }
    }
}
if ($portFromEnvFile) {
    $StreamlitPort = $portFromEnvFile
}

$sqliteUrlPath = ($SqlitePath -replace "\\", "/")
if (($sqliteUrlPath -notmatch "^[A-Za-z]:/") -and ($sqliteUrlPath -notmatch "^\\./")) {
    $sqliteUrlPath = "./$sqliteUrlPath"
}

$env:AIGM_DATABASE_URL = "sqlite:///$sqliteUrlPath"
$env:AIGM_DATABASE_SSLMODE = "require"
$env:AIGM_DATABASE_CONNECT_TIMEOUT_S = "10"
$env:AIGM_LLM_PROVIDER = "ollama"
$env:AIGM_OLLAMA_URL = $OllamaUrl
$env:AIGM_OLLAMA_MODEL = $OllamaModel

Write-Host "Starting Streamlit with local settings:"
Write-Host "  AIGM_DATABASE_URL=$($env:AIGM_DATABASE_URL)"
Write-Host "  AIGM_LLM_PROVIDER=$($env:AIGM_LLM_PROVIDER)"
Write-Host "  AIGM_OLLAMA_URL=$($env:AIGM_OLLAMA_URL)"
Write-Host "  AIGM_OLLAMA_MODEL=$($env:AIGM_OLLAMA_MODEL)"
Write-Host "  AIGM_STREAMLIT_PORT=$StreamlitPort"

& $pythonExe -m aigm.db.bootstrap --required-table campaigns --required-table system_logs --required-table bot_configs
& $pythonExe -m streamlit run streamlit_app.py --server.port $StreamlitPort
