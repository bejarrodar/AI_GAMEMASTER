param(
    [string]$AppDir = (Get-Location).Path,
    [string]$VenvPath = ".venv",
    [string]$DbName = "aigm",
    [string]$DbUser = "aigm",
    [string]$DbPassword = "aigm_password_change_me",
    [string]$OllamaModel = "qwen2.5:7b-instruct",
    [int]$StreamlitPort = 9531,
    [int]$HealthPort = 9540,
    [string]$LogDir = ".\\logs"
)

$ErrorActionPreference = "Stop"

function Set-DotEnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
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

function Ensure-WingetPackage {
    param([string]$Id)
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is required on Windows for this installer."
    }
    $installed = winget list --id $Id -e 2>$null
    if (-not $installed) {
        winget install --id $Id -e --accept-package-agreements --accept-source-agreements
    }
}

Write-Host "1/9 Installing dependencies (Python, Ollama, PostgreSQL)..."
Ensure-WingetPackage -Id "Python.Python.3.11"
Ensure-WingetPackage -Id "Ollama.Ollama"
Ensure-WingetPackage -Id "PostgreSQL.PostgreSQL"

Write-Host "2/9 Setting up Python environment..."
Push-Location $AppDir
& ".\\scripts\\setup_local_test_env.ps1" -VenvPath $VenvPath
$pythonExe = Join-Path $VenvPath "Scripts\\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found in venv: $pythonExe"
}

Write-Host "3/9 Configuring .env defaults..."
$envFile = Join-Path $AppDir ".env"
Set-DotEnvValue -Path $envFile -Key "AIGM_DATABASE_URL" -Value "postgresql+psycopg://$DbUser`:$DbPassword@localhost:5432/$DbName"
Set-DotEnvValue -Path $envFile -Key "AIGM_DATABASE_SSLMODE" -Value "prefer"
Set-DotEnvValue -Path $envFile -Key "AIGM_DATABASE_CONNECT_TIMEOUT_S" -Value "10"
Set-DotEnvValue -Path $envFile -Key "AIGM_LLM_PROVIDER" -Value "ollama"
Set-DotEnvValue -Path $envFile -Key "AIGM_OLLAMA_URL" -Value "http://127.0.0.1:11434"
Set-DotEnvValue -Path $envFile -Key "AIGM_OLLAMA_MODEL" -Value $OllamaModel
Set-DotEnvValue -Path $envFile -Key "AIGM_AUTH_ENFORCE" -Value "false"
Set-DotEnvValue -Path $envFile -Key "AIGM_STREAMLIT_PORT" -Value "$StreamlitPort"
Set-DotEnvValue -Path $envFile -Key "AIGM_HEALTHCHECK_PORT" -Value "$HealthPort"
Set-DotEnvValue -Path $envFile -Key "AIGM_HEALTHCHECK_URL" -Value "http://127.0.0.1:$HealthPort/health"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_DIR" -Value $LogDir
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_FILE_MAX_BYTES" -Value "10485760"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_FILE_BACKUP_COUNT" -Value "5"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_DB_BATCH_SIZE" -Value "50"
Set-DotEnvValue -Path $envFile -Key "AIGM_LOG_DB_FLUSH_INTERVAL_S" -Value "2"

Write-Host "4/9 Starting Ollama and pulling model..."
try {
    ollama list | Out-Null
} catch {
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 2
}
ollama pull $OllamaModel

Write-Host "5/9 Configuring PostgreSQL DB/user (best effort)..."
$psql = Get-Command psql -ErrorAction SilentlyContinue
if ($psql) {
    try {
        & psql -U postgres -h localhost -d postgres -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DbUser') THEN CREATE ROLE $DbUser LOGIN PASSWORD '$DbPassword'; END IF; END \$\$;"
        & psql -U postgres -h localhost -d postgres -c "SELECT 'CREATE DATABASE $DbName OWNER $DbUser' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DbName')\gexec"
    } catch {
        Write-Warning "Automatic PostgreSQL role/database creation failed. Create user/database manually if needed."
    }
} else {
    Write-Warning "psql not found; skipping PostgreSQL DB/user creation."
}

Write-Host "6/9 Initializing DB schema if needed..."
& $pythonExe -m aigm.db.bootstrap --required-table campaigns --required-table system_logs --required-table bot_configs

Write-Host "7/9 Writing service runner script..."
$supervisorRunner = Join-Path $AppDir "scripts\\run_supervisor_service.ps1"
$logDirPath = Join-Path $AppDir $LogDir
@"
`$ErrorActionPreference = 'Stop'
Set-Location '$AppDir'
if (-not (Test-Path '$logDirPath')) { New-Item -ItemType Directory -Force -Path '$logDirPath' | Out-Null }
& '$pythonExe' -m aigm.ops.supervisor --streamlit-port $StreamlitPort --health-port $HealthPort --log-dir '$logDirPath' --cwd '$AppDir'
"@ | Set-Content -Path $supervisorRunner -Encoding UTF8

Write-Host "8/9 Registering Windows service..."
sc.exe stop aigm-supervisor 2>$null | Out-Null
sc.exe delete aigm-supervisor 2>$null | Out-Null
$supBin = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$supervisorRunner`""
sc.exe create aigm-supervisor binPath= "$supBin" start= auto DisplayName= "AI GameMaster Supervisor" | Out-Null

Write-Host "9/9 Starting service..."
sc.exe start aigm-supervisor | Out-Null

Write-Host "Install complete."
Write-Host "Check service:"
Write-Host "  sc query aigm-supervisor"
Write-Host "  Invoke-RestMethod -Method Get -Uri http://127.0.0.1:$HealthPort/health"
Pop-Location
