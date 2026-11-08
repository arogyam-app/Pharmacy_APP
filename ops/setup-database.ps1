<#
.SYNOPSIS
    Creates the pharmacy role and database on the local PostgreSQL, and writes
    the connection string into backend\.env.

.DESCRIPTION
    Run once. It asks for two passwords and neither is ever written to the
    screen, the command line, or your PowerShell history:

      * the PostgreSQL superuser password you chose when installing it
      * a new password for the `pharmacy` role the app will use

    Safe to re-run: an existing role or database is left alone.

.EXAMPLE
    cd E:\Pharmacy_APP
    .\ops\setup-database.ps1
#>
[CmdletBinding()]
param(
    [string] $PgBin    = "C:\Program Files\PostgreSQL\18\bin",
    [string] $DbName   = "pharmacy",
    [string] $DbUser   = "pharmacy",
    [string] $PgHost   = "127.0.0.1",
    [int]    $PgPort   = 5432
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Get-PlainPassword([System.Security.SecureString] $secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try   { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

$psql = Join-Path $PgBin "psql.exe"
if (-not (Test-Path $psql)) {
    throw "psql not found at $psql. Pass -PgBin with the right path."
}

Write-Host ""
Write-Host "PostgreSQL superuser password" -ForegroundColor Cyan
Write-Host "  (the one you set when installing PostgreSQL, for the 'postgres' user)"
$superSecure = Read-Host "  Password" -AsSecureString

Write-Host ""
Write-Host "A NEW password for the application's database role" -ForegroundColor Cyan
Write-Host "  Letters and digits only -- it goes into a connection URL, where"
Write-Host "  an @ : / ? # or % would split the URL in the wrong place."
$appSecure = Read-Host "  New password for '$DbUser'" -AsSecureString

$superPlain = Get-PlainPassword $superSecure
$appPlain   = Get-PlainPassword $appSecure

if ($appPlain -notmatch '^[A-Za-z0-9]{8,}$') {
    throw "Use at least 8 characters, letters and digits only."
}

# psql reads PGPASSWORD from the environment, so the superuser password never
# appears in a command line where `ps` or the event log could pick it up.
$env:PGPASSWORD = $superPlain

try {
    & $psql -U postgres -h $PgHost -p $PgPort -d postgres -v ON_ERROR_STOP=1 -c "SELECT 1;" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not sign in to PostgreSQL as 'postgres'. Wrong password?" }
    Write-Host "`nConnected to PostgreSQL." -ForegroundColor Green

    # Role. `\gexec` runs the SQL that the SELECT produces, which is how a
    # conditional CREATE ROLE is done -- DO blocks cannot take psql variables,
    # and `format(%L)` quotes the password safely whatever is in it.
    $roleSql = @"
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '$DbUser', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DbUser')
\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', '$DbUser', :'pw')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DbUser')
\gexec
"@
    $roleSql | & $psql -U postgres -h $PgHost -p $PgPort -d postgres -v ON_ERROR_STOP=1 -v pw="$appPlain" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not create or update the '$DbUser' role." }
    Write-Host "Role '$DbUser' ready." -ForegroundColor Green

    # CREATE DATABASE cannot run inside a transaction or a DO block.
    $exists = & $psql -U postgres -h $PgHost -p $PgPort -d postgres -tAc `
        "SELECT 1 FROM pg_database WHERE datname = '$DbName';"
    if ($exists -ne "1") {
        & $psql -U postgres -h $PgHost -p $PgPort -d postgres -v ON_ERROR_STOP=1 `
            -c "CREATE DATABASE $DbName OWNER $DbUser;" *> $null
        if ($LASTEXITCODE -ne 0) { throw "Could not create the '$DbName' database." }
        Write-Host "Database '$DbName' created." -ForegroundColor Green
    } else {
        Write-Host "Database '$DbName' already existed -- left alone." -ForegroundColor Yellow
    }

    # The search indexes (migration 0002) need pg_trgm, and only a superuser
    # can install an extension.
    & $psql -U postgres -h $PgHost -p $PgPort -d $DbName -v ON_ERROR_STOP=1 `
        -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not enable the pg_trgm extension." }
    Write-Host "Extension pg_trgm enabled." -ForegroundColor Green
}
finally {
    $env:PGPASSWORD = $null
    $superPlain = $null
}

# --- write the connection string ------------------------------------------
$envFile = Join-Path $root "backend\.env"
$url = "postgresql+psycopg://$DbUser`:$appPlain@$PgHost`:$PgPort/$DbName"

$lines = @()
if (Test-Path $envFile) {
    Copy-Item $envFile "$envFile.bak" -Force
    Write-Host "`nExisting backend\.env backed up to .env.bak" -ForegroundColor Yellow
    $lines = Get-Content $envFile | Where-Object { $_ -notmatch '^\s*DATABASE_URL\s*=' }
}
$lines += "DATABASE_URL=$url"
Set-Content -Path $envFile -Value $lines -Encoding utf8

$appPlain = $null

Write-Host ""
Write-Host "backend\.env now points at the local PostgreSQL." -ForegroundColor Green
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  cd E:\Pharmacy_APP\backend"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  alembic upgrade head"
Write-Host "  python -m app.db.seed --no-demo"
Write-Host ""
