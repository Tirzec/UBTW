# Stop the bot safely:
#   1. KILL SWITCH: the bot cancels every open order and halts (waits for its confirmation)
#   2. then the process is ended.
# Open positions are NOT sold. The stop stays set: after the next start the bot stays halted
# until you run  .\windows\resume.bat  (or press Resume on the dashboard).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$vpy = Join-Path $root ".venv\Scripts\python.exe"

& $vpy -m app.trader stop --wait --reason "STOP from stop-bot.ps1"

$pidFile = Join-Path $root "data\bot.pid"
if (Test-Path $pidFile) {
    $botPid = Get-Content $pidFile | Select-Object -First 1
    $p = Get-Process -Id $botPid -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -notlike "python*") {
        Write-Host "PID $botPid is not the bot (it is $($p.ProcessName)); not touching it."
        $p = $null
    }
    if ($p) {
        Stop-Process -Id $botPid
        $p.WaitForExit(10000) | Out-Null
        Write-Host "Bot process $botPid stopped."
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
} else {
    Write-Host "No running bot found (no data\bot.pid)."
}
# also end any bot process that never finished starting (no data\bot.pid: e.g. an old background start
# waiting for the LIVE phrase). Such a process never got past its startup checks, so it holds no orders.
. (Join-Path $PSScriptRoot "bot-procs.ps1")
Stop-StuckBotProcesses $root "" | Out-Null
Write-Host "Open positions were left as they are; check them in the Polymarket app."
