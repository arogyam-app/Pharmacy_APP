# Deployment — native Windows, with a daily copy to Supabase

The UAT/beta setup: PostgreSQL and the app run directly on the counter PC, the
server starts **when the machine powers on** rather than when someone signs in,
and a copy of the database goes to Supabase three times a day so the web app
can be built against it later.

Read [Where the data lives](#2-where-the-data-lives) before anything else. It
is the part that decides whether a bad day costs you an afternoon or a year of
records.

> **This replaces the Docker setup.** That laptop cannot run Docker Desktop, so
> the whole stack is native now. The old Docker files are kept in
> `delete/docker-deployment/` rather than removed — see
> [section 12](#12-what-changed-from-the-docker-plan) for why the native
> version is arguably the better fit anyway.

---

## 1. What you are running

Two things, and only one of them is ours:

```
                     http://localhost:8000
                              |
                    +---------v----------+
                    |  uvicorn           |   Windows scheduled task,
                    |   /        -> app  |   starts AT BOOT as SYSTEM
                    |   /api/v1  -> API  |   2 workers
                    +---------+----------+
                              |
                    +---------v----------+
                    |  PostgreSQL 18     |   Windows service,
                    |  postgresql-x64-18 |   already installed, starts at boot
                    +---------+----------+
                              |
              +---------------+---------------+
              |                               |
   E:\Pharmacy_APP\data\backups        Supabase (cloud)
   plain .dump files on your disk      for the future www app
```

**One process serves both the app and the API**, on one port. That is what
makes them the same origin, so there is no CORS to configure and the httpOnly
session cookie works without a `SameSite` exception. It is also one less thing
to install, start at boot and keep patched.

The API still answers only in JSON. It additionally hands over the files
`npm run build` already produced — a delivery arrangement, not templating. See
`serve_spa` in `backend/app/core/config.py`.

**Nothing is reachable from the network.** Both the server and PostgreSQL bind
to `127.0.0.1`, so the shop wifi cannot see them even with the firewall open.

---

## 2. Where the data lives

The database exists in **three independent places**:

| Copy | Where | Survives |
|---|---|---|
| **Live** | `C:\Program Files\PostgreSQL\18\data` | reinstalling the app, everything short of uninstalling PostgreSQL |
| **Local backups** | `E:\Pharmacy_APP\data\backups\*.dump` | uninstalling PostgreSQL, reinstalling Windows if E: is untouched |
| **Supabase** | your cloud project | the laptop dying |

This is simpler than it was under Docker, and strictly safer. There is no
container to delete, no volume to prune by accident, and no `down -v` waiting
to be typed. The live data is an ordinary folder that Windows will not remove
unless you uninstall PostgreSQL.

**The backups are still the thing that matters.** A dump is a plain file any
PostgreSQL can read, long after this app is gone. Restore procedures are in
[section 9](#9-restoring).

### Copy the backups somewhere else

`E:\Pharmacy_APP\data\backups` is on the same disk as everything else, and a
dead drive takes the lot. Point OneDrive, Google Drive or a scheduled copy to a
USB disk at that folder — any of them, but do one of them.

---

## 3. What is on this machine

This is a per-machine checklist, not a one-time fact about the app — moving
the `Pharmacy_APP` folder to a different PC (a new counter machine, a
replacement after a laptop dies) means re-checking every row, because two of
them do not survive the move.

| | |
|---|---|
| **PostgreSQL 18** | service `postgresql-x64-18`, **Running**, StartType **Automatic** |
| **Python 3.12** | for the project virtualenv at `backend\.venv` |
| **Node 24** | for building the app |
| RAM / cores | varies by machine — 4+ GB and 2+ cores is comfortable for one counter |

PostgreSQL being a Windows service with StartType Automatic is the whole
reason this plan works: it is up before anyone reaches the lock screen.

**`backend\.venv` and `frontend\node_modules` do not travel.** A virtualenv
records the absolute path of the Python that built it (`backend\.venv\pyvenv.cfg`
→ `home = C:\...`); copy the folder to another machine — or even another user
account on the same machine — and every one of those paths is wrong. The
symptom is `.venv\Scripts\python.exe` failing to run, or running but unable to
import anything. There is no repair step, only rebuild.

**Do not run bare `python -m venv .venv` to do that rebuild if this machine
has more than one Python installed** — see the box below for why. Pin the
version explicitly with the `py` launcher instead:

```powershell
Remove-Item E:\Pharmacy_APP\backend\.venv -Recurse -Force
cd E:\Pharmacy_APP\backend
py -V:3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt
```

Verify before trusting it — `pip check`, and actually import the app, not
just check the folder exists:

```powershell
.\.venv\Scripts\pip.exe check
.\.venv\Scripts\python.exe -c "from app.main import app; print('OK')"
```

`node_modules` is more forgiving — it usually still works across two Windows
machines on the same CPU architecture — but if `npm run build` complains, the
fix is the same shape: delete it and `npm install` again.

### If this machine has (or ends up with) more than one Python

Modern python.org installers register through a **Python Install Manager**
(`py.exe`), which can coexist with an older, separately-installed launcher —
and with whatever `python.exe` a tool like `winget install Python.Python.3.12`
put on PATH. When more than one is present, plain `python` on PATH is
ambiguous, and **which one it resolves to can change silently** — a version
manager updating itself, or a second install landing ahead of the first one
in PATH order, redirects `python -m venv` to a completely different
interpreter without any error. The symptom is exactly what a stale venv looks
like: `alembic`/`uvicorn` missing from `.venv\Scripts`, imports failing — so
after any Python-related install or update on this machine, re-run the two
verify commands above before assuming the venv is still good.

Check what's actually resolvable and pin explicitly, every time:

```powershell
py --list                    # every interpreter the Install Manager knows about
py -V:3.12 --version         # confirm the one you want to use still is 3.12
```

If no 3.12 is listed, install one **through the Install Manager**, not
winget — it registers under its own stable, versioned path
(`%LOCALAPPDATA%\Python\pythoncore-3.12-64`) instead of a path a different
tool can silently repoint:

```powershell
py install 3.12
```

If `py` itself is missing, or Node is missing entirely, winget still works —
just open a **new** PowerShell window afterwards, since PATH is only picked
up by shells started after the install:

```powershell
winget install -e --id OpenJS.NodeJS.LTS
```

---

## 4. One-time setup

Four commands. Run them in order.

### 4.0 Let PowerShell run these scripts

Windows refuses to run *any* `.ps1` by default:

```
cannot be loaded because running scripts is disabled on this system
```

Nothing is wrong with the files — they were created here, not downloaded, so
they carry no zone mark and do not need `Unblock-File`. It is only the machine
policy, which is `Restricted` out of the box.

Allow it **for the current window only**, which changes nothing permanently:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
```

Or run one script without changing anything at all:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\Pharmacy_APP\ops\backup.ps1"
```

If you would rather not repeat it every time, this is the sane permanent
setting — local scripts run, anything downloaded still has to be signed:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

> **The scheduled tasks are not affected by any of this.** Both are registered
> with `-ExecutionPolicy Bypass` on the command line, so the server starts at
> boot whatever the machine policy says. This only concerns running the
> scripts by hand.

### 4.1 Create the database

```powershell
cd E:\Pharmacy_APP
.\ops\setup-database.ps1
```

It asks for two passwords and neither is typed on a command line, echoed to the
screen, or left in your PowerShell history:

- **the PostgreSQL superuser password** — the one you chose when installing
  PostgreSQL, for the `postgres` user;
- **a new password for the `pharmacy` role** the app will use. Letters and
  digits only: it goes into a connection URL, where an `@ : / ? # %` would
  split the URL in the wrong place and produce an error that blames the host.

It creates the role and the `pharmacy` database, enables the `pg_trgm`
extension the search indexes need, and writes `DATABASE_URL` into
`backend\.env`. Safe to re-run.

### 4.2 Build the schema and the app

```powershell
cd E:\Pharmacy_APP\backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head

cd ..\frontend
npm install
npm run build
```

### 4.3 Check the migrations on PostgreSQL — do not skip this

Everything before now has run on SQLite. This project has shipped **three**
bugs that appear only on a migrated database while every test stayed green.

```powershell
cd E:\Pharmacy_APP\backend
.\.venv\Scripts\python.exe -m pytest tests/test_migrations_postgres.py -v
```

It builds a scratch database, runs the whole chain against it, checks every
enum CHECK constraint and drops it again. Your data is not touched.

**It must pass before you enter anything real.** If it fails, fix the
migration — do not work around it, because the same fault will be waiting when
you next add one.

### 4.4 Seed the shop

```powershell
.\.venv\Scripts\python.exe -m app.db.seed --no-demo
```

`--no-demo` creates the roles, your admin user, store settings and the document
sequences, and nothing else. Drop the flag to get sample suppliers, doctors and
a catalogue to practise on.

Set the admin login first if you want something other than the default — in
`backend\.env`:

```
SEED_ADMIN_EMAIL=you@yourshop.in
SEED_ADMIN_PASSWORD=SomethingStrong@123
SEED_STORE_NAME=Your Pharmacy
SEED_STORE_STATE_CODE=18
```

### 4.5 Turn on autostart

**In an Administrator PowerShell** — a task that runs at boot needs it:

```powershell
 Set-ExecutionPolicy -Scope Process Bypass -Force
cd E:\Pharmacy_APP
.\ops\install-autostart.ps1
```

That registers two scheduled tasks and starts the server immediately, so you do
not have to reboot to use it:

| Task | When | Runs as |
|---|---|---|
| `PharmacyPOS Server` | At startup | SYSTEM |
| `PharmacyPOS Backup` | 12:00, 16:00 and 22:00 daily | SYSTEM |

### 4.6 Sign in

Open <http://localhost:8000>, sign in, then immediately:

- **Admin → Users** — change the admin password.
- **Admin → Settings → Store profile** — real name, address, GSTIN, drug
  licence numbers, phone, email. These print on every bill.
- **Admin → Settings** — discount policy, GST, invoice prefix, print format.

### 4.7 Take a backup by hand and confirm the file exists

```powershell
cd E:\Pharmacy_APP
.\ops\backup.ps1 -SkipReplicate
Get-ChildItem E:\Pharmacy_APP\data\backups
```

You should see `pharmacy-YYYYMMDD-HHMMSS.dump`. **If that folder is empty your
safety net is not connected — fix it before going live.**

---

## 5. Starting when the computer starts

Both halves are Windows-native and both start at boot:

- **PostgreSQL** — service `postgresql-x64-18`, StartType Automatic. Already
  configured by its installer.
- **The server** — scheduled task `PharmacyPOS Server`, trigger *At startup*,
  running as **SYSTEM**.

Running as SYSTEM with an at-startup trigger is what makes it come up **before
anyone signs in**. If the counter PC boots and sits on the lock screen all
morning, the shop can still take a sale from another browser on that machine
the moment someone logs in — the server has been running the whole time.

The task also restarts itself up to three times, a minute apart, if the process
dies.

### Check it survived a reboot

Restart Windows, wait a minute, then:

```powershell
Get-ScheduledTask "PharmacyPOS Server" | Select-Object TaskName, State
Invoke-WebRequest http://127.0.0.1:8000/api/v1/health -UseBasicParsing
```

`State` should be `Running` and the health check should return `{"status":"ok"}`.

---

## 6. Day to day

Run these from `E:\Pharmacy_APP`.

| What | Command |
|---|---|
| Start | `Start-ScheduledTask "PharmacyPOS Server"` |
| Stop | `Stop-ScheduledTask "PharmacyPOS Server"` |
| Is it up? | `Invoke-WebRequest http://127.0.0.1:8000/api/v1/health -UseBasicParsing` |
| Server log | `Get-Content data\logs\server-*.log -Tail 40` |
| Backup log | `Get-Content data\backups\ops.log -Tail 20` |
| Backup now | `.\ops\backup.ps1` |
| Database shell | `& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U pharmacy -h 127.0.0.1 -d pharmacy` |
| Run the tests | `cd backend; .\.venv\Scripts\python.exe -m pytest -q` |

---

## 7. Backups

`PharmacyPOS Backup` runs at **12:00, 16:00 and 22:00** — noon, mid-afternoon
and closing — and:

1. `pg_dump` in compressed custom format → `data\backups\pharmacy-<timestamp>.dump`
2. deletes any `.dump` file **older than 7 days** (age-based, not a count —
   so a day the machine was off drops off the same way, and a manual extra
   backup doesn't push a legitimate day out early)
3. pushes a copy to Supabase, if `SUPABASE_DB_URL` is set
4. appends the outcome to `data\backups\ops.log`

At three runs a day that is roughly 21 files on disk at any time.

To change the times, re-run the installer:

```powershell
.\ops\install-autostart.ps1 -BackupTimes "12:00","16:00","22:00"
```

To change how long dumps are kept, pass `-KeepDays` when running the backup
by hand or edit the default in `ops\backup.ps1`:

```powershell
.\ops\backup.ps1 -KeepDays 14
```

---

## 8. Replication to Supabase

Runs after each backup, on the same schedule. It is a **one-way overwrite**:

```
local PostgreSQL  --pg_dump-->  restore into Supabase (public schema)
```

`pg_restore --clean --if-exists` drops each object in Supabase's `public`
schema and rebuilds it from your data. Supabase's own `auth` and `storage`
schemas are untouched.

> **Supabase is a copy, not a second till.** Anything typed into it is erased
> at the next replication. The web app will read this data; it must not be
> where data is created unless you first redesign this into a two-way sync,
> which is a much larger job.

### Setting it up

1. Create a project at <https://supabase.com>. Save the database password.
2. **Project Settings → Database → Connection string → URI.** Take the
   **direct connection** (`db.<ref>.supabase.co`, port **5432**), *not* the
   pooler on 6543 — restoring a dump needs session features the transaction
   pooler does not offer, and it fails part way through.
3. **Database → Extensions** → enable **`pg_trgm`**. Migration 0002's search
   indexes need it; without it the restore completes but logs an error on
   those two indexes.
4. Add the line to `backend\.env`:

```
SUPABASE_DB_URL=postgresql://postgres:PASSWORD@db.xxxxxxxx.supabase.co:5432/postgres?sslmode=require
```

**Run one now:** `.\ops\backup.ps1`
**Turn it off:** delete that line, or use `.\ops\backup.ps1 -SkipReplicate`.

---

## 9. Restoring

Let `$PG = "C:\Program Files\PostgreSQL\18\bin"`.

### 9.1 Roll back to an earlier dump

```powershell
cd E:\Pharmacy_APP
.\ops\backup.ps1 -SkipReplicate          # keep the current state first
Stop-ScheduledTask "PharmacyPOS Server"

$env:PGPASSWORD = "<the pharmacy role password>"
& "$PG\pg_restore.exe" --clean --if-exists --no-owner --no-privileges `
    -h 127.0.0.1 -U pharmacy -d pharmacy `
    E:\Pharmacy_APP\data\backups\pharmacy-20260807-130000.dump

Start-ScheduledTask "PharmacyPOS Server"
```

Take the extra backup even though you are discarding state — if you restore the
wrong file, it is the only way back.

### 9.2 PostgreSQL is gone or the data folder is lost

Reinstall PostgreSQL, then:

```powershell
cd E:\Pharmacy_APP
.\ops\setup-database.ps1                  # recreates the role and empty database
cd backend; .\.venv\Scripts\python.exe -m alembic upgrade head

$env:PGPASSWORD = "<the pharmacy role password>"
& "$PG\pg_restore.exe" --clean --if-exists --no-owner --no-privileges `
    -h 127.0.0.1 -U pharmacy -d pharmacy <newest .dump>
```

**Do not seed after restoring.** The dump already contains the store, the users
and the document sequences; seeding on top would collide.

### 9.3 The laptop is gone

On the new machine: install PostgreSQL and Python, copy the `Pharmacy_APP`
folder and your newest `.dump` across, then follow 4.1–4.2 and 9.2.

With no local dump, pull from Supabase instead:

```powershell
& "$PG\pg_dump.exe" --no-owner --no-privileges --schema=public -Fc `
    -d "<SUPABASE_DB_URL>" -f E:\Pharmacy_APP\data\backups\from-supabase.dump
```

then restore that as in 9.2. You lose whatever happened after the last
replication.

---

## 10. Making changes during UAT

### The quick loop

Best for anything you are iterating on. Stop the service first so you are not
looking at two versions of the app and wondering which answered.

```powershell
Stop-ScheduledTask "PharmacyPOS Server"

# terminal 1 — API with reload
cd E:\Pharmacy_APP\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload

# terminal 2 — the app, with hot reload
cd E:\Pharmacy_APP\frontend
npm run dev
```

Open <http://127.0.0.1:5173>. Both talk to the same PostgreSQL the service uses,
so you are working against real data. `SERVE_SPA` stays off here, which is why
Vite's proxy handles the API.

### Shipping the change

```powershell
cd E:\Pharmacy_APP\frontend
npm run build                            # only if the frontend changed
Stop-ScheduledTask "PharmacyPOS Server"
Start-ScheduledTask "PharmacyPOS Server" # migrations run on start
```

**A schema change is a migration, always.** Never edit an applied revision:

```powershell
cd E:\Pharmacy_APP\backend
.\.venv\Scripts\Activate.ps1
alembic revision -m "what you changed"
# write the upgrade/downgrade, then restart the task
```

**Run the tests before you restart.** The suite is the only thing between a bad
afternoon and a wrong bill:

```powershell
cd E:\Pharmacy_APP\backend
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 11. When something is wrong

| Symptom | Cause and fix |
|---|---|
| `running scripts is disabled on this system` | The machine's PowerShell policy, not the file. `Set-ExecutionPolicy -Scope Process Bypass -Force` in that window — see [4.0](#40-let-powershell-run-these-scripts). The boot task is unaffected. |
| Browser: cannot reach site | `Get-ScheduledTask "PharmacyPOS Server"`. If not Running, `Start-ScheduledTask`, then read `data\logs\server-*.log`. |
| Server log says MIGRATION FAILED | An alembic error is above it. The server deliberately refuses to start on a schema it cannot bring up to date. |
| "Cannot reach the server" inside the app | The API died but the page is cached. Check the log; the task should have restarted it within a minute. |
| An endpoint 404s that used to work | The service is on older code. Restart the task. Confirm at <http://localhost:8000/openapi.json>. |
| App loads but looks old after a change | `npm run build` was not run, or not before the restart. |
| `password authentication failed` | The `pharmacy` role password in `backend\.env` no longer matches. Re-run `ops\setup-database.ps1`. |
| Backups folder empty | `Get-Content data\backups\ops.log -Tail 20`. Usually `DATABASE_URL` still points at SQLite, or the task never ran. |
| Replication fails, backups fine | Almost always the pooler URL. Use `db.<ref>.supabase.co:5432`, not `...pooler.supabase.com:6543`. |
| Everything slow after months | `& "$PG\psql.exe" -U pharmacy -h 127.0.0.1 -d pharmacy -c "VACUUM ANALYZE;"` |

---

## 12. What changed from the Docker plan

The old files are in `delete/docker-deployment/`, organised by service, in case
you ever move to a machine that can run it.

Three things got **better**, not merely different:

- **It starts at boot, not at sign-in.** Docker Desktop is a desktop app and
  waits for a login — a caveat I had to flag in the old guide. A Windows
  service and a SYSTEM scheduled task have no such limit.
- **Nothing to delete by accident.** No `docker compose down -v`, no volume to
  prune, no factory reset. The live data is an ordinary folder.
- **One process instead of four.** uvicorn serves the app and the API; nginx
  and the ops container are gone.

What got worse: nothing is isolated any more, so the Python environment and
PostgreSQL are shared with whatever else the laptop does. For one counter
machine, that is a fair trade.

---

## 13. Security, honestly

What this does:

- Nothing is published beyond `127.0.0.1` — unreachable from wifi or internet.
- Passwords are argon2 hashes; the refresh token is an httpOnly cookie page
  script cannot read.
- Both role passwords are prompted for, never passed on a command line.
- The SPA route refuses to read outside the build directory, so `../backend/.env`
  is not fetchable. There are tests for that.

What it does **not** do, and you should know before this is production:

- **No HTTPS.** Traffic is plain HTTP over loopback and never leaves the
  machine, so this is fine as it stands — and not fine the moment you expose it
  to the LAN.
- **`backend\.env` is plain text** and holds the database password and the
  signing key. Anyone who can read the folder can read them.
- **The disk is not encrypted.** Turn on BitLocker if the counter PC is not
  physically secure.
- **The server runs as SYSTEM.** That is what buys the boot-time start; it also
  means a flaw in the app runs with full machine rights. Acceptable on a
  single-purpose counter machine, not on a shared one.

---

## 14. File map

```
E:\Pharmacy_APP\
├── backend\
│   ├── .env                      DATABASE_URL, secrets   (git-ignored)
│   └── .venv\                    the virtualenv
├── frontend\
│   └── dist\                     what `npm run build` produces, served by uvicorn
├── ops\
│   ├── setup-database.ps1        one-time: role, database, pg_trgm, .env
│   ├── install-autostart.ps1     one-time: the two scheduled tasks (as Admin)
│   ├── serve.ps1                 migrate then run uvicorn — the boot task
│   └── backup.ps1                pg_dump, prune, push to Supabase
├── data\
│   ├── backups\                  .dump files + ops.log   (git-ignored)
│   └── logs\                     server-YYYYMMDD.log
└── delete\                       superseded files, kept for you to move
    ├── docker-deployment\        the previous plan, by service
    ├── alembic-versions-integer-pk\
    └── dev-database\
```
