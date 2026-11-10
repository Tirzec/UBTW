# Shared by start-bot.ps1 and stop-bot.ps1 (dot-sourced).
#
# Finds bot processes ("python -m app.main") of THIS folder that are not the bot recorded in data\bot.pid.
# Those are stuck: a bot writes data\bot.pid as soon as it is past its startup checks, so one without it never
# got that far (for example an old background start waiting, invisibly, for the LIVE confirmation phrase).
# A stuck one keeps data\console.log open, so every new background start fails.
#
# The venv's python.exe is a small launcher that starts the real interpreter as a child, so a bot is usually
# two python processes; both are matched (the child by its parent).

function Get-StuckBotProcesses([string]$root, [string]$keepPid) {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue)
    if (-not $procs) { return @() }
    $byId = @{}
    foreach ($p in $procs) { $byId[[string]$p.ProcessId] = $p }
    $isOurs = {
        param($p)
        $cl = [string]$p.CommandLine
        if ($cl.IndexOf("-m app.main", [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
        if ($cl.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
        $parent = $byId[[string]$p.ParentProcessId]                  # child of the venv launcher
        return ($parent -and ([string]$parent.CommandLine).IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    }
    # the running bot (from data\bot.pid) and its launcher/child are never touched
    $keep = @{}
    if ($keepPid) {
        $keep[$keepPid] = $true
        $kp = $byId[$keepPid]
        if ($kp) { $keep[[string]$kp.ParentProcessId] = $true }
        foreach ($p in $procs) { if ([string]$p.ParentProcessId -eq $keepPid) { $keep[[string]$p.ProcessId] = $true } }
    }
    return @($procs | Where-Object { -not $keep[[string]$_.ProcessId] -and (& $isOurs $_) })
}

function Stop-StuckBotProcesses([string]$root, [string]$keepPid) {
    $stuck = @(Get-StuckBotProcesses $root $keepPid)
    foreach ($p in $stuck) {
        Write-Host ("Stopping a stuck bot process {0} (it never finished starting; started {1})." -f `
            $p.ProcessId, $p.CreationDate) -ForegroundColor Yellow
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($stuck.Count) { Start-Sleep -Seconds 2 }
    return $stuck.Count
}
