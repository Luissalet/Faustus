#Requires -Version 5.1
param([ValidateRange(1024,65535)][int]$Port=7000,[switch]$Desktop,[switch]$NoBrowser)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimePython=Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    & (Join-Path $PSScriptRoot 'launch-windows.ps1') -SetupOnly -Port $Port
    if (-not (Test-Path -LiteralPath $runtimePython)) { throw 'Setup did not create the Python environment.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'node_modules\vite'))) {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
}
& node (Join-Path $PSScriptRoot 'scripts\build-studio.js')
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
if ($Desktop) {
    $desktopDir=Join-Path $PSScriptRoot 'desktop'
    $electronExe=Join-Path $desktopDir 'node_modules\electron\dist\electron.exe'
    if (-not (Test-Path -LiteralPath $electronExe)) {
        & npm.cmd ci --prefix $desktopDir
        if ($LASTEXITCODE -ne 0) { throw 'Desktop installation failed.' }
        & node (Join-Path $desktopDir 'node_modules\electron\install.js')
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $electronExe)) { throw 'Electron runtime installation failed.' }
    }
    $env:FAUSTUS_PORT=[string]$Port
    Start-Process -FilePath $electronExe -ArgumentList ('"'+$desktopDir+'"') -WorkingDirectory $PSScriptRoot -WindowStyle Normal
    Write-Host 'Faustus desktop launched. Close its window to stop its own server.'
} else {
    $output=& $runtimePython (Join-Path $PSScriptRoot 'server_runtime.py') start --port $Port --owner web
    if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
    $result=($output -join "`n") | ConvertFrom-Json
    if (-not $result.healthy) { throw 'The managed server is not ready. Check logs/.' }
    Write-Host "Faustus web: http://127.0.0.1:$Port - Stop-Faustus.bat stops the managed server."
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/studio" }
}
