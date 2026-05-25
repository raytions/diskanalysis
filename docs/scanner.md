# Scanner CLI Reference

The scanner runs on the **host machine** (not in Docker). It reads the filesystem and writes results to the SQLite database.

## Installation

```bash
# Install dependencies (system-wide, no venv required)
pip install -r requirements.txt --break-system-packages

# Or with a venv (Python < 3.14)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Commands

### `mounts` — list discovered mounts

```bash
python -m app.scanner mounts
```

Lists all mounts found in `/proc/mounts`, filtered by configured fs types. Shows mount point, fs type, total/used/free bytes, and whether it is a network filesystem.

### `doctor` — health check

```bash
python -m app.scanner doctor
```

Checks database connectivity, mount accessibility, and config validity. Run this first to verify the setup.

### `scan` — run a scan

```bash
# Scan all accessible mounts
python -m app.scanner scan --all-mounts

# Scan one specific mount point
python -m app.scanner scan --mount /media/hdd

# Scan a specific directory path
python -m app.scanner scan --path /media/nas02/projects

# Multiple mounts
python -m app.scanner scan --mount /media/hdd --mount /media/nas02
```

#### Scan options

| Flag | Default | Description |
|---|---|---|
| `--all-mounts` | — | Scan all accessible mounts from `/proc/mounts` |
| `--mount PATH` | — | Scan a specific mount point (can be repeated) |
| `--path PATH` | — | Scan a specific directory path (not a mount) |
| `--cold-days N` | `90` | Mark files not accessed in N days as cold |
| `--top-n N` | `100` | Max results stored per query |
| `--one-filesystem` | `true` | Stop at filesystem boundaries (xdev). **Always leave on** for NAS environments. |
| `--exclude PATH,...` | — | Additional paths or globs to exclude (comma-separated) |
| `--incremental` | `false` | Only process changed files (compares mtime) |

#### Examples

```bash
# Scan all mounts, 180-day cold threshold
python -m app.scanner scan --all-mounts --cold-days 180

# Scan /media/nas02, exclude backup dirs
python -m app.scanner scan --mount /media/nas02 --exclude /media/nas02/.trash,/media/nas02/backup

# Dry-run: just list what would be scanned
python -m app.scanner mounts

# Incremental scan (fast, only changed files)
python -m app.scanner scan --mount /media/hdd --incremental
```

## Scheduled scanning

See [Deployment](deployment.md) for systemd timer and cron setup.

## Performance notes

- The scanner streams files in batches of 500 (`BATCH_SIZE=500`) — it never holds all files in memory.
- On a 26-mount NFS server with 7.5M files, a full scan takes ~10–20 minutes.
- Incremental scans are much faster (skip unchanged files by mtime).
- After each scan, the `directories` table is rebuilt with cumulative sizes — this enables fast "largest directory" queries without scanning the `files` table.
- After each scan completes, the user attribution cache is automatically rebuilt in a background thread.
- A WAL checkpoint (`PRAGMA wal_checkpoint(PASSIVE)`) is run after each scan to prevent unbounded WAL growth when running many concurrent scans.

## atime / cold data caveats

Most Linux systems mount with `relatime` or `noatime`, which means `atime` is not updated on every read. Cold file detection (`is_cold=1`) is therefore a **best-effort indicator** — a file may appear cold even if it was recently read.

For accurate cold detection, the filesystem must be mounted without `noatime`. Check with:

```bash
cat /proc/mounts | grep noatime
```
