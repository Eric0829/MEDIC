param(
    [string]$Name = "MEDIC Observe Daemon",
    [string]$Python = "",
    [string]$Config = "",
    [double]$Interval = 60.0,
    [switch]$Apply,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$MedicRoot = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot "start_observe_daemon_hidden.ps1"
. (Join-Path $PSScriptRoot "resolve_python.ps1")
$PythonExe = Resolve-MedicPython $Python

if (-not $Config) {
    $Config = Join-Path $MedicRoot "config\observe_daemon.example.json"
}

function Quote-Arg([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$StartupDir = [Environment]::GetFolderPath("Startup")
if (-not $StartupDir) {
    $StartupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
}
$Target = Join-Path $StartupDir ($Name + ".cmd")

$CommandLine = @(
    "powershell.exe",
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", (Quote-Arg $StartScript),
    "-Python", (Quote-Arg $PythonExe),
    "-Config", (Quote-Arg $Config),
    "-Interval", [string]$Interval
) -join " "

$Result = [ordered]@{
    status = if ($Apply) { "apply" } elseif ($Remove) { "remove" } else { "dry_run" }
    startup_dir = $StartupDir
    target = $Target
    command = $CommandLine
    medic_root = $MedicRoot
    python = $PythonExe
    config = $Config
    note = "Use -Apply to create the current-user Startup entry. Use -Remove -Apply to remove it."
}

if ($Remove) {
    if ($Apply) {
        if (Test-Path -LiteralPath $Target) {
            Remove-Item -LiteralPath $Target -Force
            $Result.status = "removed"
        } else {
            $Result.status = "missing"
        }
    }
    $Result | ConvertTo-Json -Depth 6
    exit 0
}

if (-not $Apply) {
    $Result | ConvertTo-Json -Depth 6
    exit 0
}

try {
    New-Item -ItemType Directory -Force -Path $StartupDir -ErrorAction Stop | Out-Null
    $Content = @(
        "@echo off",
        $CommandLine
    ) -join "`r`n"
    Set-Content -LiteralPath $Target -Value $Content -Encoding ASCII -ErrorAction Stop
    $Result.status = "installed"
    $Result | ConvertTo-Json -Depth 6
} catch {
    $Result.status = "failed"
    $Result.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
    $Result.note = "Could not write the current-user Startup entry from this session. Try the same command in a normal Windows PowerShell session."
    $Result | ConvertTo-Json -Depth 6
    exit 1
}
