# ---------------------------------------------------------------------------
# Universal Agent Hub — نصب‌کننده‌ی ویندوز (10/11، 64-bit)
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Dev
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Extras all
#
# کارها: venv در %LOCALAPPDATA%\agent-hub\venv، نصب بسته، ساخت agent-hub.cmd در
# %LOCALAPPDATA%\agent-hub\bin (که به PATH کاربر اضافه می‌شود) و .env نمونه.
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [switch]$Dev,                       # نصب از چک‌اوت جاری (pip install -e .)
    [string]$Extras = "",               # all | browser | search | system
    [string]$Prefix = "",               # پوشه‌ی نصب
    [switch]$NoPath                     # PATH را دست نزن
)

$ErrorActionPreference = "Stop"
$Version = "1.0.0"

if (-not $Prefix) {
    $Prefix = Join-Path $env:LOCALAPPDATA "agent-hub"
}
$BinDir  = Join-Path $Prefix "bin"
$VenvDir = Join-Path $Prefix "venv"

function Say([string]$text) { Write-Host "  == $text" -ForegroundColor Cyan }
function Die([string]$text) { Write-Host "  error: $text" -ForegroundColor Red; exit 1 }

Say "python lookup"
$python = $null
foreach ($candidate in @("py", "python", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $probe = if ($candidate -eq "py") { & py -3 -c "import sys;print(sys.version_info[:2])" } else { & $candidate -c "import sys;print(sys.version_info[:2])" }
    } catch { continue }
    if (-not $probe) { continue }
    $parts = $probe -replace "[()\s]", "" -split ","
    if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) { $python = $candidate; break }
}
if (-not $python) { Die "Python 3.10+ is required — winget install Python.Python.3.12 (or python.org)" }
Say "using $python"

$pyArgs = @()
if ($python -eq "py") { $pyArgs = @("-3") }

New-Item -ItemType Directory -Force -Path $Prefix, $BinDir | Out-Null

if ($Dev) {
    $source = Split-Path -Parent $PSScriptRoot
    if (-not (Test-Path (Join-Path $source "pyproject.toml"))) { Die "--Dev needs the repo layout next to this script" }
    Set-Location $source
    $target = if ($Extras) { ".[$Extras]" } else { "." }
    Say "installing from checkout: $source"
} else {
    $target = if ($Extras) { "universal-agent-hub[$Extras]" } else { "universal-agent-hub" }
    Say "installing universal-agent-hub $Version from PyPI"
}

if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Say "creating virtualenv at $VenvDir"
    & $python @pyArgs -m venv $VenvDir
}
$venvPy = Join-Path $VenvDir "Scripts\python.exe"
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install $target
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }

# shim: agent-hub.cmd که مستقیم به venv اشاره می‌کند
$shim = Join-Path $BinDir "agent-hub.cmd"
"@echo off`r`n`"$venvPy`" -m src.cli %*`r`n" | Set-Content -Path $shim -Encoding ASCII
"@echo off`r`n`"$venvPy`" -m src.server %*`r`n" | Set-Content -Path (Join-Path $BinDir "agent-hub-server.cmd") -Encoding ASCII

# .env نمونه (کلید API همین‌جا می‌ماند و هرگز به گوشی فرستاده نمی‌شود)
$envFile = Join-Path $Prefix ".env"
if (-not (Test-Path $envFile)) {
@"
# Universal Agent Hub
OPENAI_API_KEY=
# MODEL_NAME=gpt-6-astra
# OPENAI_BASE_URL=https://.../v1
SERVER_HOST=127.0.0.1
SERVER_PORT=8765
"@ | Set-Content -Path $envFile -Encoding UTF8
    Say "wrote $envFile (کلید API را وارد کنید)"
}

if (-not $NoPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$BinDir*") {
        Say "adding $BinDir to your user PATH (open a new terminal to use it)"
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
    }
}

Say "done"
Write-Host @"

  next steps (in a new terminal)
    agent-hub --version
    agent-hub --serve --desktop        UI روی همین کامپیوتر
    agent-hub --serve --lan            اتصال گوشی — توکن چاپ‌شده را در اپ بگذارید

  config: $envFile
"@ -ForegroundColor Gray
