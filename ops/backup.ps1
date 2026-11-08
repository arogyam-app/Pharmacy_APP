<#
.SYNOPSIS
    Dumps the database to data\backups, prunes old copies, and optionally
    pushes a copy to Supabase.

.DESCRIPTION
    The dump is the safety net that depends on nothing: an ordinary file on
    your disk, readable by any PostgreSQL install, long after this app is
    gone.

    Reads DATABASE_URL and SUPABASE_DB_URL from backend\.env, so there is one
    place the connection details live.

    Runs three times a day (12:00, 16:00, 22:00) and keeps the last 7 days
    of dumps, pruned by age -- see -KeepDays.

.EXAMPLE
    .\ops\backup.ps1                 # dump, prune, replicate
    .\ops\backup.ps1 -SkipReplicate  # local only
#>
[CmdletBinding()]
param(
    [string] $PgBin     = "C:\Program Files\PostgreSQL\18\bin",
    # Backups run three times a day (12:00, 16:00, 22:00), so 7 days is ~21
    # files. Age-based, not count-based: a day the machine was off still
    # drops off the same way, and a burst of manual backups does not push
    # a legitimate day out early.
    [int]    $KeepDays  = 7,
    [switch] $SkipReplicate
)

# "Stop" for our own cmdlets; native commands are handled by Invoke-Native
# below. pg_dump and pg_restore write progress and warnings to stderr, and
# under "Stop" the first such line would be a terminating error -- the exact
# fault that stopped serve.ps1 dead. Exit code is the only failure signal.
$ErrorActionPreference = "Stop"

function Invoke-Native([string] $exe, [string[]] $arguments, [string] $stderrFile) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($stderrFile) { & $exe @arguments 2> $stderrFile } else { & $exe @arguments }
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
}

$root      = Split-Path -Parent $PSScriptRoot
$backupDir = Join-Path $root "data\backups"
$envFile   = Join-Path $root "backend\.env"
$log       = Join-Path $backupDir "ops.log"

if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir -Force | Out-Null }

function Write-Log([string] $message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

function Get-EnvValue([string] $key) {
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^\s*$key\s*=\s*(.+?)\s*$") { return $Matches[1] }
    }
    return $null
}

<#
    postgresql+psycopg://user:pass@host:port/db  ->  the parts pg_dump needs.
    SQLAlchemy's driver suffix is not a thing libpq understands.
#>
function ConvertFrom-DatabaseUrl([string] $url) {
    $clean = $url -replace '^postgresql\+\w+://', 'postgresql://'
    $uri = [System.Uri] $clean
    $userInfo = $uri.UserInfo -split ':', 2
    [pscustomobject]@{
        PgHost   = $uri.Host
        Port     = if ($uri.Port -gt 0) { $uri.Port } else { 5432 }
        User     = [System.Uri]::UnescapeDataString($userInfo[0])
        Password = if ($userInfo.Count -gt 1) { [System.Uri]::UnescapeDataString($userInfo[1]) } else { "" }
        Database = $uri.AbsolutePath.TrimStart('/')
    }
}

$pgDump    = Join-Path $PgBin "pg_dump.exe"
$pgRestore = Join-Path $PgBin "pg_restore.exe"
if (-not (Test-Path $pgDump)) { throw "pg_dump not found at $pgDump" }

$databaseUrl = Get-EnvValue "DATABASE_URL"
if (-not $databaseUrl) { throw "DATABASE_URL is not set in $envFile" }
if ($databaseUrl -like "sqlite*") { throw "DATABASE_URL still points at SQLite; run ops\setup-database.ps1 first." }

$db = ConvertFrom-DatabaseUrl $databaseUrl

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$file  = Join-Path $backupDir "pharmacy-$stamp.dump"

Write-Log "backup starting -> $(Split-Path -Leaf $file)"

$env:PGPASSWORD = $db.Password
try {
    # Custom format: compressed, and pg_restore can be selective about it.
    # --schema=public keeps this to the app's own tables, which is also what
    # makes the same dump safe to push into Supabase beside its own schemas.
    $code = Invoke-Native $pgDump @(
        "--format=custom", "--no-owner", "--no-privileges", "--schema=public",
        "-h", $db.PgHost, "-p", "$($db.Port)", "-U", $db.User, "-d", $db.Database, "-f", $file
    )
    if ($code -ne 0) {
        Remove-Item $file -ErrorAction SilentlyContinue
        Write-Log "ERROR: pg_dump failed; partial file removed"
        exit 1
    }
    $size = (Get-Item $file).Length
    Write-Log "backup ok -- $size bytes"

    # Keep the last $KeepDays days. A failed dump is already gone, so
    # everything here is a file that completed.
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    $stale = Get-ChildItem $backupDir -Filter "pharmacy-*.dump" | Where-Object { $_.LastWriteTime -lt $cutoff }
    foreach ($old in $stale) {
        Remove-Item $old.FullName -Force
        Write-Log "pruned $($old.Name) (older than $KeepDays days)"
    }

    # --- Supabase ---------------------------------------------------------
    #
    # ONE WAY, AND DESTRUCTIVE AT THE FAR END. The local database is the only
    # source of truth; --clean drops each object in Supabase's public schema
    # before recreating it. Anything typed into Supabase between runs is
    # erased. It is a copy for the future web app to read, not a second till.
    if ($SkipReplicate) {
        Write-Log "replication skipped (-SkipReplicate)"
        exit 0
    }
    $supabase = Get-EnvValue "SUPABASE_DB_URL"
    if (-not $supabase) {
        Write-Log "replication skipped -- SUPABASE_DB_URL is not set"
        exit 0
    }

    Write-Log "replication starting"
    $far = ConvertFrom-DatabaseUrl $supabase
    $env:PGPASSWORD = $far.Password
    $errFile = Join-Path $env:TEMP "pharmacy-restore.err"

    $code = Invoke-Native $pgRestore @(
        "--clean", "--if-exists", "--no-owner", "--no-privileges",
        "-h", $far.PgHost, "-p", "$($far.Port)", "-U", $far.User, "-d", $far.Database, $file
    ) $errFile
    if ($code -ne 0) {
        Write-Log "ERROR: pg_restore into Supabase failed:"
        Get-Content $errFile -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Log "  $_" }
        # The local backup is what matters; a failed push is not a failed run.
        exit 0
    }
    Write-Log "replication ok"
}
finally {
    $env:PGPASSWORD = $null
}
