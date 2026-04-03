param(
    [string]$VenvPath = ".venv",
    [string]$SqlitePath = ".\aigm_local.db",
    [string]$OllamaModel = "qwen2.5:7b-instruct",
    [int]$StreamlitPort = 9531,
    [int]$HealthPort = 9540,
    [string]$LogDir = ".\logs",
    [switch]$SkipTests,
    [switch]$SkipOllamaInstall
)

$ErrorActionPreference = "Stop"

function Set-DotEnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )

    if (-not (Test-Path $Path)) {
        Set-Content -Path $Path -Value "" -Encoding UTF8
    }

    $lines = @(Get-Content $Path)
    if ($lines.Count -eq 1 -and [string]::IsNullOrWhiteSpace($lines[0])) {
        $lines = @()
    }
    $updated = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$([regex]::Escape($Key))=") {
            $lines[$i] = "$Key=$Value"
            $updated = $true
            break
        }
    }
    if (-not $updated) {
        $lines += , ("$Key=$Value")
    }
    Set-Content -Path $Path -Value $lines -Encoding UTF8
}

function Ensure-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Invoke-DbEnsureAndHeal {
    param(
        [string]$PythonExe,
        [int]$MaxAttempts = 4
    )

    $attempt = 1
    while ($attempt -le $MaxAttempts) {
        try {
            Write-Host "  [db] ensure/heal attempt $attempt/$MaxAttempts ..."
            & $PythonExe -m aigm.db.bootstrap
            if ($LASTEXITCODE -ne 0) {
                throw "db.bootstrap exited with code $LASTEXITCODE"
            }

            $verifyScript = @'
from sqlalchemy import inspect
from aigm.db.session import engine

inspector = inspect(engine)
if not inspector.has_table("campaigns"):
    raise SystemExit("campaigns table missing after bootstrap")
columns = {c["name"] for c in inspector.get_columns("campaigns")}
if "version" not in columns:
    raise SystemExit("campaigns.version is still missing after bootstrap")
print("[startup] database schema verified (campaigns.version present)")
'@
            $verifyScript | & $PythonExe -
            if ($LASTEXITCODE -ne 0) {
                throw "schema verification failed with code $LASTEXITCODE"
            }
            return
        }
        catch {
            if ($attempt -ge $MaxAttempts) {
                throw "Database ensure/heal failed after $MaxAttempts attempts. $($_.Exception.Message)"
            }
            Write-Warning "Database ensure/heal attempt $attempt failed: $($_.Exception.Message)"
            Write-Warning "Retrying in 2 seconds. Ensure Streamlit/bot processes are stopped and DB file is not locked."
            Start-Sleep -Seconds 2
            $attempt++
        }
    }
}

Write-Host "1/9 Setting up Python environment..."
& ".\scripts\setup_local_test_env.ps1" -VenvPath $VenvPath

$pythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found in virtual environment: $pythonExe"
}

Write-Host "2/9 Preparing local SQLite database path..."
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

Write-Host "3/9 Configuring .env for local stack..."
$envFile = ".env"
Set-DotEnvValue -Path $envFile -Key "AIGM_DATABASE_URL" -Value "sqlite:///$sqliteUrlPath"
Set-DotEnvValue -Path $envFile -Key "AIGM_DATABASE_SSLMODE" -Value "require"
Set-DotEnvValue -Path $envFile -Key "AIGM_DATABASE_CONNECT_TIMEOUT_S" -Value "10"
Set-DotEnvValue -Path $envFile -Key "AIGM_LLM_PROVIDER" -Value "ollama"
Set-DotEnvValue -Path $envFile -Key "AIGM_OLLAMA_URL" -Value "http://127.0.0.1:11434"
Set-DotEnvValue -Path $envFile -Key "AIGM_OLLAMA_MODEL" -Value $OllamaModel
Set-DotEnvValue -Path $envFile -Key "AIGM_STREAMLIT_PORT" -Value "$StreamlitPort"
Set-DotEnvValue -Path $envFile -Key "AIGM_HEALTHCHECK_PORT" -Value "$HealthPort"
Set-DotEnvValue -Path $envFile -Key "AIGM_HEALTHCHECK_URL" -Value "http://127.0.0.1:$HealthPort/health"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_DIR" -Value $LogDir
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_FILE_MAX_BYTES" -Value "10485760"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_FILE_BACKUP_COUNT" -Value "5"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_DB_BATCH_SIZE" -Value "50"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_DB_FLUSH_INTERVAL_S" -Value "2"

# Ensure current process uses the same values even if .env has stale formatting/content.
$env:AIGM_DATABASE_URL = "sqlite:///$sqliteUrlPath"
$env:AIGM_DATABASE_SSLMODE = "require"
$env:AIGM_DATABASE_CONNECT_TIMEOUT_S = "10"
$env:AIGM_LLM_PROVIDER = "ollama"
$env:AIGM_OLLAMA_URL = "http://127.0.0.1:11434"
$env:AIGM_OLLAMA_MODEL = $OllamaModel
$env:AIGM_STREAMLIT_PORT = "$StreamlitPort"
$env:AIGM_HEALTHCHECK_PORT = "$HealthPort"
$env:AIGM_HEALTHCHECK_URL = "http://127.0.0.1:$HealthPort/health"
$env:AIGM_LOG_DIR = $LogDir
$env:AIGM_LOG_FILE_MAX_BYTES = "10485760"
$env:AIGM_LOG_FILE_BACKUP_COUNT = "5"
$env:AIGM_LOG_DB_BATCH_SIZE = "50"
$env:AIGM_LOG_DB_FLUSH_INTERVAL_S = "2"

Write-Host "4/9 Verifying Ollama..."
$ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollamaCmd) {
    if ($SkipOllamaInstall) {
        throw "Ollama is not installed and -SkipOllamaInstall was used."
    }
    Ensure-Command -Name winget
    winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollamaCmd) {
        $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
        if (Test-Path $candidate) {
            $env:Path = "$($env:Path);$([System.IO.Path]::GetDirectoryName($candidate))"
        }
    }
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollamaCmd) {
        throw "Ollama install finished but the ollama command is still unavailable. Restart shell and rerun."
    }
}

try {
    ollama list | Out-Null
}
catch {
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Write-Host "5/9 Pulling Ollama model: $OllamaModel ..."
ollama pull $OllamaModel

Write-Host "6/9 Validating database schema and backend defaults..."
Invoke-DbEnsureAndHeal -PythonExe $pythonExe -MaxAttempts 4

$seedValidationScript = @'
from aigm.adapters.llm import LLMAdapter
from aigm.db.session import SessionLocal
from aigm.services.game_service import GameService

service = GameService(LLMAdapter())
with SessionLocal() as db:
    service.seed_default_auth(db)
    service.seed_default_agency_rules(db)
    service.seed_default_gameplay_knowledge(db)

print("[startup] backend defaults validated")
'@

$seedValidationScript | & $pythonExe -

if (-not $SkipTests) {
    Write-Host "Running lint + tests..."
    & ".\scripts\run_tests.ps1" -VenvPath $VenvPath
}

Write-Host "7/9 Validating Discord bot token..."
$discordToken = $env:AIGM_DISCORD_TOKEN
if (-not $discordToken -and (Test-Path ".env")) {
    $tokenLine = Get-Content ".env" | Where-Object { $_ -match "^AIGM_DISCORD_TOKEN=" } | Select-Object -First 1
    if ($tokenLine) {
        $discordToken = $tokenLine.Substring("AIGM_DISCORD_TOKEN=".Length).Trim()
    }
}
if ($discordToken) {
    try {
        $resp = Invoke-RestMethod `
            -Uri "https://discord.com/api/v10/users/@me" `
            -Headers @{ Authorization = "Bot $discordToken" } `
            -Method Get `
            -TimeoutSec 15
        Write-Host "Logged in as $($resp.username)#$($resp.discriminator) (id=$($resp.id))"
    } catch {
        throw "Discord token validation failed. Check AIGM_DISCORD_TOKEN. Error: $($_.Exception.Message)"
    }
} else {
    Write-Host "AIGM_DISCORD_TOKEN not set. Skipping Discord login validation."
}

Write-Host "8/9 Ensuring log directory exists..."
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

Write-Host "9/9 Starting unified supervisor (bot manager + streamlit + health api + unified logs)..."
& $pythonExe -m aigm.ops.supervisor --streamlit-port $StreamlitPort --health-port $HealthPort --log-dir $LogDir --cwd (Get-Location).Path
