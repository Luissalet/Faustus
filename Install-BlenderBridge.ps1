param([string]$BlenderPath = '', [string]$PackagesPath = "$env:USERPROFILE\Downloads")
$ErrorActionPreference = 'Stop'
if (-not $BlenderPath) {
    $lts = Join-Path $env:ProgramFiles 'Blender Foundation\Blender 5.2\blender.exe'
    if (Test-Path -LiteralPath $lts) {
        $BlenderPath = $lts
    } else {
        $found = Get-ChildItem (Join-Path $env:ProgramFiles 'Blender Foundation') -Filter blender.exe -Recurse -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { $BlenderPath = $found.FullName }
    }
}
if (-not $BlenderPath -or -not (Test-Path -LiteralPath $BlenderPath)) { throw 'Choose an installed Blender 5.1+ executable with -BlenderPath' }
$python = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the Faustus Python environment first.' }
$installer = Join-Path $PSScriptRoot 'src\blender_live\install.py'
& $python $installer --blender $BlenderPath --packages $PackagesPath
if ($LASTEXITCODE -ne 0) { throw 'Blender add-on installation failed' }
Write-Output 'Installed the official Blender MCP distribution and native Faustus workflows. Open Blender and use the Faustus sidebar.'
