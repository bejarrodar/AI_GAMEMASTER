param(
    [string]$AppDir = (Get-Location).Path,
    [switch]$SkipLocalPostgresInstall,
    [switch]$SkipLocalOllamaInstall
)

& ".\scripts\install_windows_stack.ps1" `
    -AppDir $AppDir `
    -Components "bot" `
    -SkipLocalPostgresInstall:$SkipLocalPostgresInstall `
    -SkipLocalOllamaInstall:$SkipLocalOllamaInstall
