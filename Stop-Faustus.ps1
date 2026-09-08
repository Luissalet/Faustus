#Requires -Version 5.1
$ErrorActionPreference='Stop'
$runtimePython=Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) { throw 'No installed environment in this checkout.' }
$output=& $runtimePython (Join-Path $PSScriptRoot 'server_runtime.py') stop
if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
$result=($output -join "`n") | ConvertFrom-Json
if ($result.stopped) { Write-Host 'Faustus stopped. Unrelated Python processes and external model servers were preserved.' }
else { Write-Host 'No server owned by these launchers was stopped. A server launched elsewhere must be closed by its own launcher.' }
