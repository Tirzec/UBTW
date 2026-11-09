# Start the bot + dashboard.
#   .\windows\start-bot.ps1            runs in the background (keeps running after you close PowerShell)
#   .\windows\start-bot.ps1 -Visible   runs in its own window (REQUIRED for LIVE mode: the live
#                                      check asks you to type a confirmation phrase)
#   add -Https if your browser forces https for localhost
param([switch]$Visible, [switch]$Https)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$vpy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { Write-Host "Run .\windows\setup.ps1 first." -ForegroundColor Red; exit 1 }

$pidFile = Join-Path $root "data\bot.pid"
if (Test-Path $pidFile) {
    $old = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $op = if ($old) { Get-Process -Id $old -ErrorAction SilentlyContinue }
    if ($op -and $op.ProcessName -like "python*") {
        Write-Host "The bot is already running (PID $old). Dashboard: http://localhost:8000"
        exit 0
    }
}
Remove-Item $pidFile -ErrorAction SilentlyContinue        # stale file from a crash / power cut
New-Item -ItemType Directory -Force (Join-Path $root "data") | Out-Null
$appArgs = "-m app.main"
if ($Https) { $appArgs += " --https" }

if ($Visible) {
    # its own console window: closing that window stops the bot
    Start-Process -FilePath "cmd.exe" -WorkingDirectory $root `
        -ArgumentList "/k", "title BTC signals bot && `"$vpy`" $appArgs"
    Write-Host "Started in a new window. Keep that window open (you can minimize it)."
    exit 0
}

# Hidden console of its own, so closing this PowerShell window does not stop the bot.
$log = Join-Path $root "data\console.log"
Start-Process -FilePath "cmd.exe" -WorkingDirectory $root -WindowStyle Hidden `
    -ArgumentList "/c", "`"`"$vpy`" $appArgs --no-browser > `"$log`" 2>&1`""
Write-Host "Starting in the background ..."
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $pidFile) {
        $botPid = Get-Content $pidFile | Select-Object -First 1
        if (Get-Process -Id $botPid -ErrorAction SilentlyContinue) { break }
    }
}
Start-Sleep -Seconds 3
if (-not (Test-Path $pidFile)) {
    Write-Host "The bot did not start. Last lines of data\console.log:" -ForegroundColor Red
    Get-Content $log -Tail 25 -ErrorAction SilentlyContinue
    exit 1
}
$botPid = Get-Content $pidFile | Select-Object -First 1
$scheme = if ($Https) { "https" } else { "http" }
Write-Host "Running (PID $botPid). Dashboard: ${scheme}://localhost:8000   Log: data\console.log" -ForegroundColor Green
Write-Host "Stop it with: .\windows\stop-bot.ps1"
Start-Process "${scheme}://localhost:8000"
