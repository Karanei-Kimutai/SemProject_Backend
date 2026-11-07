<#
.SYNOPSIS
    Sets up the local stack on Windows for building the Android app.

.DESCRIPTION
    Expects the three repositories side by side in one folder:

        <parent>\SemProject_MLEngine
        <parent>\SemProject_Backend          (this script is in scripts\)
        <parent>\VolatileDemandForecastingMobileApplication

    It does the following, skipping anything already done, so it is safe to re-run:
      1. Checks Docker, Git, Python and the folders.
      2. Creates SemProject_Backend\.env with local settings and random secrets (an existing one is kept).
      3. Creates the ML engine's Python environment and exports the demo data and sample shops.
      4. Builds and starts the stack (db, ml, api, worker) and waits for it.
      5. Loads the demo store (about 7 minutes, first run only).
      6. Creates your research user (you type the password).
      7. Adds a CLAUDE.md to the app project if it has none.

.EXAMPLE
    cd SemProject_Backend
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1 -ResearchEmail you@example.com
#>
[CmdletBinding()]
param(
    # Email for the research user. If left out, the script asks (leave the answer blank to skip).
    [string]$ResearchEmail,
    # Folder name of the Android project, next to SemProject_Backend.
    [string]$AppFolder = "VolatileDemandForecastingMobileApplication",
    # Don't create the Python environment or re-export the sample shops.
    [switch]$SkipPython,
    # Load the demo store again even if it is already loaded.
    [switch]$ReseedDemo
)

$ErrorActionPreference = "Stop"
$Backend = Split-Path -Parent $PSScriptRoot
$Parent = Split-Path -Parent $Backend
$ML = Join-Path $Parent "SemProject_MLEngine"
$App = Join-Path $Parent $AppFolder
$EnvFile = Join-Path $Backend ".env"
$Reference = Join-Path $ML "data\processed\reference"
$Raw = Join-Path $ML "data\raw"
$ComposeFiles = @("compose", "-f", "docker-compose.yml", "-f", "docker-compose.dev.yml")

# ---- helpers ------------------------------------------------------------------------------

function Step([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Note([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }
function Fail([string]$Text) {
    Write-Host ""
    Write-Host "SETUP STOPPED: $Text" -ForegroundColor Red
    exit 1
}

# Runs a command with all output hidden; returns $true if it exited with 0.
function Test-Command([scriptblock]$Block) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block *> $null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
    finally { $ErrorActionPreference = $previous }
}

# Runs a native command with visible output and stops the setup if it fails.
function Invoke-Checked([string]$What, [string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)." }
}

function Invoke-Compose([string]$What, [string[]]$Arguments) {
    Push-Location $Backend
    try { Invoke-Checked $What "docker" ($ComposeFiles + $Arguments) }
    finally { Pop-Location }
}

# One value from the database ("" if the query fails). SQL must use single quotes only.
function Get-SqlValue([string]$Sql) {
    Push-Location $Backend
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & docker @ComposeFiles exec -T db psql -U forecast -d forecast -tAc $Sql 2> $null
        if ($LASTEXITCODE -ne 0) { return "" }
        return ($out | Out-String).Trim()
    }
    finally { $ErrorActionPreference = $previous; Pop-Location }
}

function Get-EnvValue([string]$Name, [string]$Default) {
    if (-not (Test-Path $EnvFile)) { return $Default }
    $line = Get-Content $EnvFile | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
    if (-not $line) { return $Default }
    $value = ($line -split "=", 2)[1].Trim()
    if ($value) { return $value } else { return $Default }
}

function New-Secret([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    return -join ($buffer | ForEach-Object { $_.ToString("x2") })
}

function Set-EnvLine([string]$Text, [string]$Name, [string]$Value) {
    $pattern = "(?m)^#?[ \t]*$Name=.*$"
    if ([regex]::IsMatch($Text, $pattern)) {
        return ([regex]$pattern).Replace($Text, "$Name=$Value", 1)
    }
    return $Text.TrimEnd() + "`n$Name=$Value`n"
}

# ---- 1. prerequisites -----------------------------------------------------------------------

Step "Checking tools and folders"
foreach ($tool in @("docker", "git")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "$tool is not installed or not on PATH." }
}
if (-not (Test-Command { docker info })) {
    Fail "Docker isn't running. Start Docker Desktop, wait until it says 'Engine running', then re-run this script."
}
if (-not (Test-Command { docker compose version })) { Fail "Docker Compose v2 is missing. Update Docker Desktop." }
Ok "Docker is running"

if (-not (Test-Path (Join-Path $ML "mlengine"))) {
    Fail "$ML has no mlengine folder. Clone SemProject_MLEngine next to SemProject_Backend (branch model-implementation until it's merged)."
}
$branch = ((& git -C $ML branch --show-current) | Out-String).Trim()
Ok "ML engine found (branch $branch)"
if (Test-Path $App) { Ok "App project found: $App" } else { Note "App project not found at $App (the CLAUDE.md step will be skipped)." }

$rawFiles = @("calendar.csv", "sales_train_evaluation.csv", "sell_prices.csv")
$hasRaw = -not ($rawFiles | Where-Object { -not (Test-Path (Join-Path $Raw $_)) })
$hasReference = Test-Path (Join-Path $Reference "store.json")
if ($hasRaw) { Ok "Raw M5 data found" } else { Note "Raw M5 data missing in $Raw (copy data\raw from WSL to refresh the exports)." }
if (-not $hasRaw -and -not $hasReference) {
    Fail "Neither the raw M5 data ($Raw) nor the demo export ($Reference) exists. Copy SemProject_MLEngine\data from WSL (\\wsl$\<distro>\home\<you>\SemProject_MLEngine\data) and re-run."
}

$Python = $null
if (-not $SkipPython) {
    foreach ($candidate in @(@("py", "-3.12"), @("py", "-3"), @("python"))) {
        $exe = $candidate[0]
        $rest = @($candidate | Select-Object -Skip 1)
        if ((Get-Command $exe -ErrorAction SilentlyContinue) -and (Test-Command { & $exe @rest -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" })) {
            $Python = $candidate
            break
        }
    }
    if ($Python) { Ok "Python found ($($Python -join ' '))" } else { Note "Python 3.10+ not found: the sample shops won't be refreshed." }
}

# ---- 2. backend .env ------------------------------------------------------------------------

Step "Backend settings (.env)"
if (Test-Path $EnvFile) {
    Ok "Keeping the existing .env"
} else {
    $postgresPassword = New-Secret 16
    $text = (Get-Content (Join-Path $Backend ".env.example") -Raw) -replace "`r`n", "`n"
    $text = Set-EnvLine $text "API_DOMAIN" "localhost"
    $text = Set-EnvLine $text "POSTGRES_PASSWORD" $postgresPassword
    $text = Set-EnvLine $text "DATABASE_URL" "postgresql+psycopg://forecast:$postgresPassword@db:5432/forecast"
    $text = Set-EnvLine $text "JWT_SECRET" (New-Secret 32)
    $text = Set-EnvLine $text "ML_SERVICE_TOKEN" (New-Secret 24)
    $text = Set-EnvLine $text "DB_PORT" "55433"
    $text = Set-EnvLine $text "API_PORT" "8000"
    [System.IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding $false))
    Ok "Created .env with random local secrets"
    if (Test-Command { docker volume inspect semproject_backend_pgdata }) {
        Note "A database from an earlier attempt exists with a different password. If the API can't start, run:"
        Note "  docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v   (wipes local data), then re-run."
    }
}
$ApiPort = Get-EnvValue "API_PORT" "8000"

# ---- 3. ML engine Python environment and exports ---------------------------------------------

if ($Python) {
    Step "ML engine Python environment and data exports"
    $venvPython = Join-Path $ML ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        $exe = $Python[0]
        $rest = @($Python | Select-Object -Skip 1)
        Invoke-Checked "Creating the Python environment" $exe ($rest + @("-m", "venv", (Join-Path $ML ".venv")))
    }
    if (-not (Test-Command { & $venvPython -c "import mlengine, lightgbm, sklearn" })) {
        Write-Host "    Installing the ML engine package (a few minutes the first time)..."
        Invoke-Checked "Upgrading pip" $venvPython @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
        Invoke-Checked "Installing mlengine" $venvPython @("-m", "pip", "install", "--quiet", "-e", $ML)
    }
    Ok "Python environment ready"
    Push-Location $ML
    try {
        if ($hasRaw -and -not $hasReference) {
            Write-Host "    Exporting the demo store (a few minutes)..."
            Invoke-Checked "Exporting the demo store" $venvPython @("-m", "mlengine", "export-reference")
            $hasReference = $true
        }
        if ($hasRaw) {
            Invoke-Checked "Exporting the sample shops" $venvPython @("-m", "mlengine", "export-samples")
            Ok "Sample shops written to $ML\data\processed\samples (history ends yesterday)"
        }
    }
    finally { Pop-Location }
}
if (-not $hasReference) { Fail "The demo export is missing ($Reference). Install Python 3.10+ and re-run, or copy data\processed from WSL." }

# ---- 4. start the stack -----------------------------------------------------------------------

Step "Building and starting the backend (the first build takes several minutes)"
Invoke-Compose "Starting the stack" @("up", "-d", "--build")
Write-Host "    Waiting for the API on http://localhost:$ApiPort ..."
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://localhost:$ApiPort/healthz"
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}
if (-not $healthy) {
    Fail "The API didn't come up within 3 minutes. Check: docker compose -f docker-compose.yml -f docker-compose.dev.yml logs api"
}
Ok "API is up"

$mlCheck = "import os, httpx; r = httpx.get(os.environ['ML_SERVICE_URL'] + '/internal/v1/health', headers={'X-Internal-Token': os.environ['ML_SERVICE_TOKEN']}, timeout=10); print(r.status_code)"
Push-Location $Backend
try { $mlStatus = ($mlCheck | & docker @ComposeFiles exec -T api python - 2> $null | Out-String).Trim() }
finally { Pop-Location }
if ($mlStatus -eq "200") { Ok "ML service is reachable from the API" }
else { Fail "The API can't reach the ML service (got '$mlStatus'). Check: docker compose -f docker-compose.yml -f docker-compose.dev.yml logs ml" }

# ---- 5. demo store ----------------------------------------------------------------------------

Step "Demo store"
$seeded = Get-SqlValue "select exists(select 1 from sales_daily s join stores st on st.id = s.store_id join workspaces w on w.id = st.workspace_id where w.is_reference)"
if ($seeded -eq "t" -and -not $ReseedDemo) {
    Ok "Already loaded (use -ReseedDemo to load it again)"
} else {
    Write-Host "    Loading 2.8 million sales rows (about 7 minutes)..."
    Invoke-Compose "Loading the demo store" @("run", "--rm", "-v", "${Reference}:/seed:ro", "api", "python", "-m", "app.cli", "seed-reference", "/seed")
    Ok "Demo store loaded"
}

# ---- 6. research user -------------------------------------------------------------------------

Step "Research user"
if (-not $PSBoundParameters.ContainsKey("ResearchEmail")) {
    $ResearchEmail = Read-Host "    Email for your research user (leave blank to skip)"
}
$ResearchEmail = "$ResearchEmail".Trim().ToLower()
if (-not $ResearchEmail) {
    Note "Skipped. Later: docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm api python -m app.cli create-user you@example.com --research"
} elseif ($ResearchEmail -notmatch "^[^@\s']+@[^@\s']+\.[^@\s']+$") {
    Note "'$ResearchEmail' doesn't look like an email address; skipped."
} elseif ((Get-SqlValue "select is_research from users where email = '$ResearchEmail'") -eq "t") {
    Ok "$ResearchEmail already exists as a research user"
} else {
    Write-Host "    Choose a password (at least 10 characters; typing is hidden):"
    Invoke-Compose "Creating the research user" @("run", "--rm", "api", "python", "-m", "app.cli", "create-user", $ResearchEmail, "--research")
}

# ---- 7. CLAUDE.md in the app project ------------------------------------------------------------

if (Test-Path $App) {
    Step "Claude instructions in the app project"
    $claudeMd = Join-Path $App "CLAUDE.md"
    if (Test-Path $claudeMd) {
        Ok "CLAUDE.md already exists (left unchanged)"
        if (-not (Select-String -Path $claudeMd -Pattern "Co-Authored-By" -Quiet)) {
            Note "It doesn't mention the no Co-Authored-By rule; consider adding it."
        }
    } else {
        $content = @"
# Project instructions

- Read ../SemProject_MLEngine/MOBILE_APP_HANDOFF.md before starting work. Then read the design spec it links to and ../SemProject_Backend/docs/api.md.
- Never add Co-Authored-By trailers or "Generated with Claude Code" lines to commits or pull requests.
- Do not modify ../SemProject_Backend or ../SemProject_MLEngine. Raise any API contract problems with the project owner.
- The backend runs locally in Docker at http://localhost:$ApiPort (from the emulator: http://10.0.2.2:$ApiPort). Emails such as password reset codes appear in ``docker compose logs api``, run in ../SemProject_Backend.
"@
        [System.IO.File]::WriteAllText($claudeMd, ($content -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
        Ok "Created CLAUDE.md (not committed; review it and commit it yourself)"
    }
}

# ---- done -----------------------------------------------------------------------------------------

Step "Done"
$products = Get-SqlValue "select count(*) from products p join workspaces w on w.id = p.workspace_id where w.is_reference"
Ok "Demo store: $products products"
Ok "API:      http://localhost:$ApiPort   (docs: http://localhost:$ApiPort/docs)"
Ok "Emulator: http://10.0.2.2:$ApiPort   (USB phone: adb reverse tcp:${ApiPort} tcp:${ApiPort}, then http://localhost:$ApiPort)"
Write-Host ""
Write-Host "Still for you to do:"
Write-Host "  1. In the emulator's browser, open http://10.0.2.2:$ApiPort/healthz to check the app can reach the API."
Write-Host "  2. Start Claude in the app project:"
Write-Host "       cd ..\$AppFolder"
Write-Host "       claude --add-dir ..\SemProject_MLEngine --add-dir ..\SemProject_Backend"
Write-Host "  3. Stop the stack when you're done (data is kept):"
Write-Host "       docker compose -f docker-compose.yml -f docker-compose.dev.yml down"
