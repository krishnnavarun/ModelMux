<#
    ModelMux launcher.

        .\run.ps1              start Redis if needed, start the API, open the console
        .\run.ps1 -Mock        same, but with fake providers (no API keys needed)
        .\run.ps1 -Test        run the test suite and exit
        .\run.ps1 -Eval        run the live held-out evaluation and exit
        .\run.ps1 -NoBrowser   don't open a browser

    Everything it does, it says. Nothing here is required to run the project --
    the raw commands are in SETUP.md and still work.
#>
[CmdletBinding()]
param(
    [switch]$Mock,
    [switch]$Test,
    [switch]$Eval,
    [switch]$NoBrowser,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$PY   = 'C:\dev\modelmux-venv\Scripts\python.exe'
$ROOT = $PSScriptRoot
Set-Location $ROOT

function Say  ($m) { Write-Host "  $m" }
function Ok   ($m) { Write-Host "  [ok]   $m"   -ForegroundColor Green }
function Warn ($m) { Write-Host "  [warn] $m"   -ForegroundColor Yellow }
function Bad  ($m) { Write-Host "  [fail] $m"   -ForegroundColor Red }

Write-Host ""
Write-Host "  ModelMux" -ForegroundColor Cyan -NoNewline
Write-Host "  cost-aware LLM routing"
Write-Host "  ------------------------------------------------------------"

# --- 1. interpreter --------------------------------------------------------
if (-not (Test-Path $PY)) {
    Bad "No interpreter at $PY"
    Say "The virtualenv lives OUTSIDE this folder on purpose (1.1 GB of"
    Say "regenerable binaries do not belong in a OneDrive-synced directory)."
    Say "Recreate it with:"
    Say "    python -m venv C:\dev\modelmux-venv"
    Say "    C:\dev\modelmux-venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}
Ok "interpreter"

# --- 2. tests / eval short-circuits ----------------------------------------
if ($Test) {
    Write-Host ""
    & $PY -m pytest tests/ -q
    exit $LASTEXITCODE
}

# --- 3. API keys -- report properties, NEVER values ------------------------
# Printing a secret is how this project leaked one once. Length and last four
# characters are enough to tell two keys apart and useless to a reader.
$envReport = & $PY -c @"
from dotenv import dotenv_values
v = dotenv_values('.env')
for k in ('GROQ_API_KEY','GOOGLE_API_KEY','ANTHROPIC_API_KEY'):
    val = v.get(k)
    print(f'{k}|{\"\" if not val else str(len(val))}|{\"\" if not val else val[-4:]}')
"@ 2>$null

$have = @{}
foreach ($line in $envReport -split "`n") {
    $p = $line.Trim() -split '\|'
    if ($p.Count -ge 3 -and $p[1]) { $have[$p[0]] = $p[2] }
}

if ($Mock) {
    $env:MODELMUX_MOCK_PROVIDERS = "1"
    Warn "MOCK PROVIDERS -- every answer is canned. Nothing below is real."
    Say  "Run 'wsl redis-cli FLUSHDB' afterwards: mock runs poison the cache."
} else {
    foreach ($k in 'GROQ_API_KEY','GOOGLE_API_KEY') {
        if ($have.ContainsKey($k)) { Ok "$k set (ends ...$($have[$k]))" }
        else { Warn "$k missing -- tiers using it will fail" }
    }
    if (-not $have.ContainsKey('GROQ_API_KEY')) {
        Say "  small + mid are Groq. Get a key: https://console.groq.com/keys"
    }
    if (-not $have.ContainsKey('GOOGLE_API_KEY')) {
        Say "  large falls back to Groq without it. Free key:"
        Say "  https://aistudio.google.com/apikey"
    }

    # The D29 trap: an environment variable silently beats .env, so a stale
    # one means edits to .env do nothing and every call 401s.
    foreach ($k in 'GROQ_API_KEY','GOOGLE_API_KEY','ANTHROPIC_API_KEY') {
        $proc = [Environment]::GetEnvironmentVariable($k, 'Process')
        $user = [Environment]::GetEnvironmentVariable($k, 'User')
        if ($proc -or $user) {
            Warn "$k is set in the ENVIRONMENT -- it overrides .env (D29)."
            Say  "  clear it:  [Environment]::SetEnvironmentVariable('$k',`$null,'User')"
            Say  "  then open a NEW terminal -- this one keeps its copy."
        }
    }
}

# --- 4. Redis --------------------------------------------------------------
# NOTE: this only PINGS. It deliberately does not try to start redis.
# `wsl -e sudo service redis-server start` prompts for a password when sudo
# is not passwordless, and a launcher that blocks on an invisible prompt is
# worse than one that tells you what to type. Found by hitting it.
$redis = $false
try {
    $ping = Start-Job { (wsl redis-cli PING 2>$null) -join '' }
    if (Wait-Job $ping -Timeout 5) { $redis = (Receive-Job $ping).Trim() -eq 'PONG' }
    Remove-Job $ping -Force -ErrorAction SilentlyContinue
} catch {}

if ($redis) {
    Ok "redis"
} else {
    Warn "redis is not answering -- the semantic cache will be OFF"
    Say  "  everything else still works; you just lose cache hits."
    Say  "  start it in another window (it may ask for your WSL password):"
    Say  "      wsl -e sudo service redis-server start"
}

# --- 5. eval short-circuit (needs redis decision above) --------------------
if ($Eval) {
    if ($redis) { Say "flushing cache so old answers cannot contaminate the run"; wsl redis-cli FLUSHDB | Out-Null }
    Write-Host ""
    & $PY eval/run_eval.py --set holdout.json
    exit $LASTEXITCODE
}

# --- 6. port --------------------------------------------------------------
$inUse = $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($inUse) {
    Bad "port $Port is already in use -- another server is probably running"
    Say "either use it at http://localhost:$Port/  or start this one with -Port 8001"
    exit 1
}

# --- 7. go ----------------------------------------------------------------
Write-Host "  ------------------------------------------------------------"
Say "console  ->  http://localhost:$Port/"
Say "api docs ->  http://localhost:$Port/docs"
Say "stop     ->  Ctrl+C"
Write-Host ""
Say "first start takes ~20s: it loads MiniLM and embeds 60 labelled examples"
Write-Host ""

if (-not $NoBrowser) {
    # Open once the port answers, so the browser never lands on a dead socket.
    Start-Job -ScriptBlock {
        param($p)
        1..90 | ForEach-Object {
            Start-Sleep -Milliseconds 700
            try {
                if ((Invoke-WebRequest "http://localhost:$p/health" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) {
                    Start-Process "http://localhost:$p/"; break
                }
            } catch {}
        }
    } -ArgumentList $Port | Out-Null
}

& $PY -m uvicorn app.main:app --port $Port --reload
