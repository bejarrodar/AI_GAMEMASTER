param(
    [string]$AppDir = (Get-Location).Path
)

& ".\scripts\install_windows_stack.ps1" `
    -AppDir $AppDir `
    -Components "llm" `
    -SkipLocalPostgresInstall `
    -SkipServiceInstall
