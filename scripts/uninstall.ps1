# uninstall.ps1 - remove the Faustus install on Windows.
#
# Default (no switches): stops the Faustus scheduled task/service if
# present and leaves data\, .env and backups\ untouched.
#
# -Purge: ALSO deletes data\, .env and backups\ under this checkout. Only
# runs after an explicit -Purge switch; there is no other path to that
# deletion in this script.
#
# See docs/distribution/LIFECYCLE.md for the full lifecycle this script is
# one step of.
param(
    [switch]$Purge,
    [switch]$KeepData,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Write-Host "Uninstalling Faustus from $RepoRoot ..."

$taskName = "FaustusLauncher"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "[+] Stopping and removing scheduled task $taskName"
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
} else {
    Write-Host "[i] No $taskName scheduled task found; skipping."
}

if ($Purge) {
    if (-not $Yes) {
        $confirm = Read-Host "This will PERMANENTLY delete data\, .env and backups\ under $RepoRoot. Type 'yes' to continue"
        if ($confirm -ne "yes") {
            Write-Host "Aborted; nothing under data\ was deleted."
            exit 1
        }
    }
    Write-Host "[+] -Purge: deleting data\, .env, backups\"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $RepoRoot "data")
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $RepoRoot ".env")
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $RepoRoot "backups")
} else {
    Write-Host "[i] Keeping data\, .env and backups\ (pass -Purge to delete them)."
}

Write-Host "Done."
