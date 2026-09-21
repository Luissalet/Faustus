#Requires -Version 5.1
param([switch]$Elevated,[switch]$NoElevation)
$ErrorActionPreference='Stop'
$runtimePython=Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) { throw 'No installed environment in this checkout.' }

$output=& $runtimePython (Join-Path $PSScriptRoot 'server_runtime.py') stop-all
if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
$result=($output -join "`n") | ConvertFrom-Json

if ($result.remaining.Count -gt 0 -and -not $Elevated -and -not $NoElevation) {
    Write-Host 'Some Faustus processes require administrator permission. Retrying elevated...'
    $arguments=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),'-Elevated')
    try {
        $elevatedProcess=Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList $arguments
        if ($elevatedProcess.ExitCode -ne 0) { exit $elevatedProcess.ExitCode }
        exit 0
    } catch {
        Write-Error 'Administrator permission was not accepted; Faustus is still running.'
        exit 2
    }
}

if ($result.remaining.Count -gt 0) {
    Write-Error ("Could not stop Faustus PIDs: " + ($result.remaining -join ', '))
    exit 2
}
if ($result.stopped) {
    $pids=if ($result.pids.Count -gt 0) { ' PIDs: '+($result.pids -join ', ')+'.' } else { '' }
    Write-Host ("Faustus stopped completely."+$pids)
} else {
    Write-Host 'Faustus was not running.'
}
Write-Host 'Ollama, llama-server, and unrelated Python processes were preserved.'
