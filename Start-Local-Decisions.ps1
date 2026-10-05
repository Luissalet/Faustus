param([string]$Root = 'D:\LocalAI\ollaya', [switch]$Stop, [switch]$Warm)
$ErrorActionPreference = 'Stop'
# The executable is the checksum-verified official v0.9.0 distribution.
# No downloads, startup registration or replacement of the default helper.
$decisionExe = [IO.Path]::GetFullPath((Join-Path $Root 'v0.9.0\bin\ollaya.exe'))
function Initialize-LocalDecisionModel {
    if (-not $Warm) { return }
    $decisionTags = Invoke-RestMethod 'http://127.0.0.1:11435/api/tags' -TimeoutSec 5
    if ('laya:multilingual' -notin $decisionTags.models.name) { throw 'The multilingual model is not downloaded. No automatic download is attempted.' }
    $decisionPayload = @{ model='laya:multilingual'; state='Ready for a local diagnostic.'; questions=@{ warm=@{ type='noul'; criteria=@{ true='The text is a greeting'; false='The text is not a greeting' } } } } | ConvertTo-Json -Depth 5 -Compress
    Invoke-RestMethod 'http://127.0.0.1:11435/v1/systemone' -Method Post -ContentType 'application/json' -Body $decisionPayload -TimeoutSec 30 | Out-Null
    Write-Output 'Downloaded multilingual model warmed on CPU; Faustus default helper unchanged.'
}
if (-not (Test-Path -LiteralPath $decisionExe)) { throw "Missing local Ollaya executable: $decisionExe" }
$decisionListener = Get-NetTCPConnection -State Listen -LocalPort 11435 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($decisionListener) {
    $decisionProcess = Get-Process -Id $decisionListener.OwningProcess
    if ($decisionProcess.Path -ne $decisionExe) { throw 'Port 11435 belongs to another process.' }
    if ($Stop) { Stop-Process -Id $decisionProcess.Id; Write-Output 'Local decisions stopped.'; return }
    Write-Output 'Local decisions already running on http://127.0.0.1:11435'; Initialize-LocalDecisionModel; return
}
if ($Stop) { Write-Output 'Local decisions are already stopped.'; return }
$decisionLog = Join-Path $Root 'logs'
New-Item -ItemType Directory -Path $decisionLog -Force | Out-Null
$decisionKeys = @('OLLAYA_HOST', 'OLLAYA_MODELS', 'OLLAYA_DEVICE', 'OLLAYA_MAX_LOADED_MODELS', 'OLLAYA_KEEP_ALIVE', 'OLLAYA_LOG_DIR')
$decisionOld = @{}
foreach ($decisionKey in $decisionKeys) { $decisionOld[$decisionKey] = [Environment]::GetEnvironmentVariable($decisionKey, 'Process') }
try {
    $env:OLLAYA_HOST = '127.0.0.1:11435'
    $env:OLLAYA_MODELS = Join-Path $Root 'models'
    $env:OLLAYA_DEVICE = 'cpu'
    $env:OLLAYA_MAX_LOADED_MODELS = '1'
    $env:OLLAYA_KEEP_ALIVE = '-1'
    $env:OLLAYA_LOG_DIR = $decisionLog
    $decisionChild = Start-Process -FilePath $decisionExe -ArgumentList 'serve' -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $decisionLog 'serve.stdout.log') -RedirectStandardError (Join-Path $decisionLog 'serve.stderr.log')
} finally {
    foreach ($decisionKey in $decisionKeys) { [Environment]::SetEnvironmentVariable($decisionKey, $decisionOld[$decisionKey], 'Process') }
}
foreach ($decisionAttempt in 1..30) {
    try {
        $decisionVersion = Invoke-RestMethod 'http://127.0.0.1:11435/api/version' -TimeoutSec 2
        Write-Output "Local decisions ready (PID $($decisionChild.Id), $($decisionVersion.version)), CPU only. Default Faustus helper unchanged."
        Initialize-LocalDecisionModel
        return
    } catch { if ($decisionChild.HasExited) { throw 'Local decisions exited; inspect its local logs.' }; Start-Sleep -Milliseconds 500 }
}
throw 'Local decisions did not become ready. Inspect its local logs.'
