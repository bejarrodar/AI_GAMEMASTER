param()

$ErrorActionPreference = "Stop"

function Assert-Contains {
    param(
        [string]$Path,
        [string]$Needle
    )
    $raw = Get-Content $Path -Raw
    if ($raw -notmatch [regex]::Escape($Needle)) {
        throw "Missing expected token '$Needle' in $Path"
    }
}

Write-Host "Validating PowerShell installer syntax..."
[void][scriptblock]::Create((Get-Content "scripts/install_windows_stack.ps1" -Raw))
[void][scriptblock]::Create((Get-Content "scripts/install_bot_stack.ps1" -Raw))
[void][scriptblock]::Create((Get-Content "scripts/install_web_stack.ps1" -Raw))
[void][scriptblock]::Create((Get-Content "scripts/install_llm_stack.ps1" -Raw))
[void][scriptblock]::Create((Get-Content "scripts/install_db_stack.ps1" -Raw))

Write-Host "Validating Linux installer flags and component scripts..."
Assert-Contains "scripts/install_cloud_stack.sh" "COMPONENTS="
Assert-Contains "scripts/install_cloud_stack.sh" "INSTALL_LOCAL_POSTGRES="
Assert-Contains "scripts/install_cloud_stack.sh" "INSTALL_LOCAL_OLLAMA="
Assert-Contains "scripts/install_cloud_stack.sh" "RUN_DB_BOOTSTRAP="
Assert-Contains "scripts/install_cloud_stack.sh" "INSTALL_SERVICE="
Assert-Contains "scripts/install_bot_stack.sh" "COMPONENTS=bot"
Assert-Contains "scripts/install_web_stack.sh" "COMPONENTS=web"
Assert-Contains "scripts/install_llm_stack.sh" "COMPONENTS=llm"
Assert-Contains "scripts/install_db_stack.sh" "COMPONENTS:-db"

Write-Host "Installer validation checks passed."
