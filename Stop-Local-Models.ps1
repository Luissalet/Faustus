# Stop the local model processes used by this Faustus installation.
# The llama.cpp watchdog must go first, or it will restart the large model.
$ErrorActionPreference = 'Stop'
$localAiRoot = Split-Path -Parent $PSScriptRoot
$serverExe = Join-Path $localAiRoot 'llama.cpp\llama-server.exe'
$watchdogScript = Join-Path $localAiRoot 'Start-LlamaServer.ps1'
$problems = @()

$watchdogs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('powershell.exe', 'pwsh.exe') -and
    $_.CommandLine -and $_.CommandLine.Contains($watchdogScript)
})
foreach ($watchdog in $watchdogs) {
    try {
        Stop-Process -Id $watchdog.ProcessId -Force -ErrorAction Stop
        Write-Output "Stopped llama.cpp watchdog PID $($watchdog.ProcessId)"
    } catch {
        $problems += "watchdog PID $($watchdog.ProcessId): $_"
    }
}

foreach ($port in @(8081, 8082)) {
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not $process -or $process.ExecutablePath -ine $serverExe) { continue }
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            Write-Output "Stopped Faustus llama-server PID $($process.ProcessId) on port $port"
        } catch {
            $problems += "llama-server PID $($process.ProcessId): $_"
        }
    }
}

$ollamaListener = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue
if ($ollamaListener) {
    try {
        $loaded = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/ps' -TimeoutSec 5
        foreach ($item in @($loaded.models)) {
            $model = [string]$item.name
            if ($model -notmatch '^qwen3\.8:') { continue }
            $body = @{ model = $model; keep_alive = 0 } | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/generate' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 20 | Out-Null
            Write-Output "Unloaded Ollama model $model"
        }
    } catch {
        $problems += "Ollama model unload: $_"
    }
}

if ($problems.Count -gt 0) { throw ($problems -join '; ') }
