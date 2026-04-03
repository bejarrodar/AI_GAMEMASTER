param(
    [string]$VenvPath = ".venv",
    [string]$SqlitePath = ".\aigm_local.db",
    [string]$OllamaUrl = "http://127.0.0.1:11434",
    [string]$OllamaModel = "llama3.2:3b"
)

$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Virtual environment not found. Run .\scripts\setup_local_test_env.ps1 first."
}

if (-not $env:AIGM_DISCORD_TOKEN) {
    if (Test-Path ".env") {
        $tokenLine = Get-Content ".env" | Where-Object { $_ -match "^AIGM_DISCORD_TOKEN=" } | Select-Object -First 1
        if ($tokenLine) {
            $env:AIGM_DISCORD_TOKEN = $tokenLine.Substring("AIGM_DISCORD_TOKEN=".Length).Trim()
        }
    }
}

if (-not $env:AIGM_DISCORD_TOKEN) {
    throw "AIGM_DISCORD_TOKEN is not set. Put it in .env or current shell env."
}

$parentDir = Split-Path -Path $SqlitePath -Parent
if ([string]::IsNullOrWhiteSpace($parentDir)) {
    $parentDir = "."
}
if (-not (Test-Path $parentDir)) {
    New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
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

Write-Host "Starting Discord bot manager with local settings..."
& $pythonExe -m aigm.db.bootstrap --required-table campaigns --required-table system_logs --required-table bot_configs
& $pythonExe -m aigm.ops.bot_manager
