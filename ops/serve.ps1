<#
.SYNOPSIS
    Starts the Pharmacy POS server. Run by the boot task; also fine by hand.

.DESCRIPTION
    One process serves both the API and the built Vue app on one port, so the
    page and the API are the same origin -- no CORS, and the session cookie
    works without exception. See app/core/config.py:serve_spa.

    Every path here is absolute. A scheduled task at boot starts in
    C:\Windows\System32, where nothing relative resolves.

    ON NATIVE COMMANDS AND STDERR -- the reason this script is shaped as it is:
    alembic and uvicorn both write their ordinary INFO logging to stderr.
    Piping that through `2>&1 | Add-Content` makes PowerShell 5.1 wrap each
    line in an ErrorRecord, and under `$ErrorActionPreference = "Stop"` the
    first such line is a TERMINATING error. The earlier version of this script
    did exactly that: it logged "starting", died on alembic's first INFO line,
    and left no trace of why. Streams go to the log by file redirection
    (`*>>`) instead, and the exit code is the only thing treated as failure.
#>
[CmdletBinding()]
param(
    [int]    $Port    = 8000,
    # Loopback only. Bind to 0.0.0.0 and the shop wifi can reach it over plain
    # HTTP, which needs a certificate first.
    [string] $Address = "127.0.0.1",
    [int]    $Workers = 2
)

$root    = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$python  = Join-Path $backend ".venv\Scripts\python.exe"
$logDir  = Join-Path $root "data\logs"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir ("server-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-Log([string] $message) {
    "[{0}] {1}" -f (Get-Date -Format s), $message | Add-Content -Path $log -Encoding utf8
}

<#
    Runs a native command, appends what it printed to the log, and returns its
    exit code.

    Redirection happens at the *process* level rather than through PowerShell's
    pipeline. Piping gives two problems at once: stderr lines come back wrapped
    as ErrorRecords, so the log fills with "At serve.ps1:52 char:9 + & $exe"
    noise around every ordinary INFO line; and `*>>` writes in the host's
    default encoding, which lands as UTF-16 next to the UTF-8 this script
    writes and renders as spaced-out gibberish. Start-Process hands the child
    real file handles, so the log is just what the program printed.
#>
function Invoke-Logged([string] $exe, [string[]] $arguments) {
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $exe -ArgumentList $arguments `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        foreach ($file in @($outFile, $errFile)) {
            Get-Content $file -ErrorAction SilentlyContinue |
                Where-Object { $_ -match '\S' } |
                ForEach-Object { "    $_" | Add-Content -Path $log -Encoding utf8 }
        }
        return $process.ExitCode
    }
    finally {
        Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

# Everything below is wrapped so a failure is *written down*. A boot task that
# dies silently is the hardest kind of problem to find: there is no window to
# see it in and no console to print to.
try {
    Write-Log "=========================================================="
    Write-Log ("starting on {0}:{1} with {2} worker(s)" -f $Address, $Port, $Workers)

    if (-not (Test-Path $python)) {
        throw "No virtualenv at $python. Run: python -m venv .venv"
    }

    # Refuse to start if something already holds the port.
    #
    # Without this the failure is silent and deeply confusing: uvicorn cannot
    # bind, exits, and the process already there keeps answering. The app
    # looks fine and serves the *old* code, so every change appears not to
    # have taken effect -- which is exactly how a stale server survived three
    # restarts and a 50,000-row limit kept reporting 5,000.
    $held = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($held) {
        $pids = ($held.OwningProcess | Sort-Object -Unique) -join ", "
        throw ("port $Port is already in use by PID $pids -- stop it first, or this " +
               "would leave the old server answering with the old code. " +
               "Elevated: taskkill /PID $pids /F")
    }

    Set-Location $backend

    # Migrations run on every start, so the schema can never be older than the
    # code about to serve it. Seeding is deliberately separate -- it happens
    # once, by hand.
    Write-Log "applying migrations"
    $code = Invoke-Logged $python @("-m", "alembic", "upgrade", "head")
    if ($code -ne 0) {
        throw "alembic exited $code -- refusing to serve a schema it could not bring up to date"
    }
    Write-Log "migrations ok"

    $env:SERVE_SPA = "true"

    $dist = Join-Path $root "frontend\dist\index.html"
    if (-not (Test-Path $dist)) {
        # Not fatal: the API is still usable and this says why the page is not.
        Write-Log "WARNING: $dist is missing -- run 'npm run build' in frontend\"
    }

    # uvicorn gets its own file, written live by the process itself. Capturing
    # it the way `Invoke-Logged` does would hold every line until the server
    # exits -- which for something meant to run for months means an empty log
    # exactly when you need to read it.
    #
    # A timestamp in the name rather than just the date, because
    # Start-Process truncates what it redirects to, and a restart would
    # otherwise erase the log of the crash that caused it.
    $runStamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $outLog = Join-Path $logDir "uvicorn-$runStamp.out.log"
    $errLog = Join-Path $logDir "uvicorn-$runStamp.err.log"

    Write-Log "handing over to uvicorn (output in uvicorn-$runStamp.*.log)"

    # Keep a fortnight of them; they are small and this runs unattended.
    Get-ChildItem $logDir -Filter "uvicorn-*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue

    $server = Start-Process -FilePath $python -NoNewWindow -Wait -PassThru `
        -ArgumentList @(
            "-m", "uvicorn", "app.main:app",
            "--host", $Address,
            "--port", "$Port",
            "--workers", "$Workers",
            "--no-access-log"
        ) `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog

    Write-Log "uvicorn exited $($server.ExitCode)"
    # Non-zero tells the scheduled task to use its restart policy.
    exit $server.ExitCode
}
catch {
    Write-Log ("FAILED: " + $_.Exception.Message)
    Write-Log ("  at " + $_.InvocationInfo.PositionMessage -replace "`r?`n", " ")
    exit 1
}
