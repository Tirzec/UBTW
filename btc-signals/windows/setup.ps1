# One-time setup on Windows. Run from PowerShell:   .\windows\setup.ps1
# (or double-click windows\setup.bat)
# "Continue" (not "Stop"): Windows PowerShell 5.1 turns any stderr text from a native program
# (pip notices, the Store's python stub) into a terminating error. Exit codes are checked instead.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Find Python 3.9+ (prefer the "py" launcher that the python.org installer adds).
# Tested versions (3.13 / 3.12) come first: the newest Python may not have ready-made
# packages (wheels) for every dependency yet.
$exe = $null; $extra = @(); $v = $null
foreach ($cand in "py -3.13", "py -3.12", "py -3", "python") {
    $parts = $cand -split " "
    if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
    $args2 = @($parts | Select-Object -Skip 1)
    $out = & $parts[0] @args2 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out -and ([version]"$out" -ge [version]"3.9")) {
        $exe = $parts[0]; $extra = $args2; $v = "$out"; break
    }
}
if (-not $exe) {
    Write-Host "Python 3.9+ not found. Install it from https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "(tick 'Add python.exe to PATH' in the installer), then open a NEW PowerShell window."
    exit 1
}
Write-Host "Using Python $v ($exe $extra)"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment .venv ..."
    & $exe @extra -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
$vpy = Join-Path $root ".venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example (edit it with: notepad .env)"
}

Write-Host "`nRunning tests ..."
& $vpy -m pytest -q
Write-Host "`nChecking the PC clock ..."
& $vpy -m app.clock
Write-Host "`nSetup done. Start the bot with:  .\windows\start-bot.ps1" -ForegroundColor Green
