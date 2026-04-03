param(
    [string]$AppDir = (Get-Location).Path,
    [string]$DbName = "aigm",
    [string]$DbUser = "aigm",
    [string]$DbPassword = "aigm_password_change_me",
    [switch]$SkipLocalPostgresInstall
)

$ErrorActionPreference = "Stop"

$args = @(
    "-AppDir", $AppDir,
    "-Components", "db",
    "-DbName", $DbName,
    "-DbUser", $DbUser,
    "-DbPassword", $DbPassword,
    "-SkipLocalOllamaInstall",
    "-SkipServiceInstall"
)

if ($SkipLocalPostgresInstall) {
    $args += "-SkipLocalPostgresInstall"
}

& "$PSScriptRoot\install_windows_stack.ps1" @args
