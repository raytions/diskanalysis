# DiskAnalysis

A self-hosted disk analysis web panel for Linux servers with many NAS mount points.

**Architecture:** host-side scanner (Python CLI) + web panel (FastAPI + Jinja2, SQLite).

---

## Features

- **Mount management** — auto-discover system mounts; manually register NFS/SMB/sshfs mounts; live mount status (green/red dot); one-click mount/unmount from the detail page; SSH ControlMaster multi-path commands (single password for multiple sshfs mounts)
- **Scanning** — full or incremental scan; per-mount, all mounts, or arbitrary path; configurable cold-data threshold and exclude rules; background thread execution with live progress; stop running scans
- **By File / By Directory** — largest files and directories, sortable and searchable
- **Cold Data** — files not accessed in N days, filterable by directory prefix
- **By Extension** — aggregate disk usage by file type
- **By User** — per-user disk usage with NFS4 uid-squash support; depth-1 and depth-2 directory matching; cached after first computation; pre-warmed automatically after each scan
- **Settings** — sidebar navigation; confirmed user list with aliases; candidate sync from scan data; path attribution toggle; attribution cache management
- **Auth** — optional session-based admin authentication

---

## Quick Start

### 1. Install scanner dependencies on the host

```bash
# Python 3.14+: no venv available, install system-wide
pip install -r requirements.txt --break-system-packages

# Or with a venv (Python < 3.14)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set DB_PATH to a path both the scanner and web container can reach
```

### 2. Run a first scan

```bash
# List discovered mounts
python -m app.scanner mounts

# Health check
python -m app.scanner doctor

# Scan all mounts
python -m app.scanner scan --all-mounts

# Scan one mount
python -m app.scanner scan --mount /mnt/nas1

# Scan a specific path, 180-day cold threshold
python -m app.scanner scan --path /mnt/nas1/projectA --cold-days 180

# Exclude paths or globs
python -m app.scanner scan --mount /mnt/nas1 --exclude /mnt/nas1/.trash,*.tmp
```

### 3. Start the web panel

```bash
cp .env.example .env
mkdir -p data
docker compose up -d
# Open: http://localhost:8000
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `APP_PORT` | `8000` | Web panel port |
| `DB_PATH` | `/data/diskanalysis.db` | SQLite database path |
| `DEFAULT_COLD_DAYS` | `90` | Default cold-data threshold (days) |
| `DEFAULT_TOP_N` | `100` | Default result count per tab |
| `EXCLUDE_PATHS` | `/proc,/sys,/dev,/run,/snap` | Always-excluded paths |
| `NETWORK_FS_TYPES` | `nfs,nfs4,cifs,smb3,...` | FS types treated as network mounts |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `PATH_USER_ATTRIBUTION` | `true` | Attribute files by top-level directory name before uid (useful for NFS4 uid-squash). Can also be toggled in the Settings UI. |
| `ADMIN_PASSWORD` | `` | If set, enables session-based admin auth. Leave empty for open access. |

---

## Scheduled Scanning (systemd)

```bash
sudo cp deploy/diskscan.service /etc/systemd/system/
sudo cp deploy/diskscan.timer /etc/systemd/system/
# Edit WorkingDirectory and DB_PATH in diskscan.service
sudo systemctl daemon-reload
sudo systemctl enable --now diskscan.timer
sudo systemctl status diskscan.timer
```

### Cron alternative

```cron
# /etc/cron.d/diskanalysis
0 2 * * *   root  cd /opt/diskanalysis && .venv/bin/python -m app.scanner scan --all-mounts
0 1 * * 0   root  cd /opt/diskanalysis && .venv/bin/python -m app.scanner scan --mount /mnt/nas1 --cold-days 90
```

---

## Reverse Proxy (Nginx)

```nginx
server {
    listen 80;
    server_name diskanalysis.internal;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Design Notes

### Why the scanner runs on the host (not in Docker)

The scanner needs direct access to `/proc/mounts`, real filesystem metadata (`atime`, inode, `st_dev`), and must handle permission escalation. Running it in a container requires mounting the entire host filesystem, which is both risky and complicates xdev (filesystem boundary) detection.

**Recommended approach:** run the scanner as root (or a privileged user) on the host via cron/systemd. Share only the SQLite database file with the web container via a bind mount.

### If you must containerise the scanner

```yaml
volumes:
  - /:/host:ro
  - /proc:/proc:ro
```

Set `SCAN_ROOTS=/host/mnt` in `.env`. Note that `shutil.disk_usage` and `/proc/mounts` paths will differ inside the container.

### NAS mount scanning risks

- **Stale mounts** (NFS): `os.stat` on a stale NFS mount can block for minutes. The scanner catches and logs these errors — a stale mount will appear as inaccessible.
- **noatime / relatime**: Most Linux systems mount with `relatime` or `noatime`. Cold file data is a best-effort indicator, not an absolute fact.
- **Permission errors**: Network filesystems may deny access depending on server configuration. Errors are logged per-path and do not abort the scan.

### Why `--one-filesystem` matters

Without xdev protection, scanning `/mnt` on a server with 30 NAS mounts would recursively descend into all of them. `--one-filesystem` (default: on) uses `os.stat().st_dev` to detect filesystem boundaries and skips them. Always leave this on in NAS environments.

### SSH ControlMaster for sshfs multi-path mounts

When a manually-registered sshfs mount has multiple remote paths, the UI generates a single-password mount sequence using SSH ControlMaster:

```bash
# Enter password once to open a shared background connection:
ssh -fNM -o StrictHostKeyChecking=no -o ControlPath=/tmp/sshctl-user@host -o ControlPersist=120 user@host

# Mount all paths (no further password needed):
sshfs -o ControlPath=/tmp/sshctl-user@host user@host:/remote/path1 /local/path1
sshfs -o ControlPath=/tmp/sshctl-user@host user@host:/remote/path2 /local/path2
```

### By User — NFS4 uid-squash problem

NFS4 `all_squash` mounts assign a single uid to every file regardless of actual ownership. Path-based attribution (`PATH_USER_ATTRIBUTION=true`) matches top-level directory names against a curated user list (with depth-2 fallback for intermediate container directories), then falls back to uid for unmatched files. Each file is counted exactly once. See [docs/user-attribution.md](docs/user-attribution.md) for details.

---

## Documentation

- [Architecture](docs/architecture.md) — system design, data flow, why scanner runs on host
- [Configuration](docs/configuration.md) — full `.env` reference
- [Scanner CLI](docs/scanner.md) — all scanner commands and flags
- [Deployment](docs/deployment.md) — Docker, systemd, nginx, reverse proxy
- [User Attribution](docs/user-attribution.md) — per-user disk usage, NFS4 uid-squash, cache behaviour

---

## Running Tests

```bash
python -m pytest tests/ -v
```

---

## Project Structure

```
diskanalysis/
├── app/
│   ├── main.py              # FastAPI app entry point + lifespan
│   ├── config.py            # Settings (pydantic-settings, reads .env)
│   ├── db.py                # SQLAlchemy engine, WAL mode, migrations
│   ├── models.py            # ORM models (Mount, Scan, File, Directory, …)
│   ├── schemas.py           # Pydantic API schemas + ok()/err() helpers
│   ├── auth.py              # Session-based admin authentication
│   ├── scanner.py           # CLI entry point (argparse)
│   ├── api/
│   │   ├── routes_mounts.py   # /api/mounts/* endpoints
│   │   ├── routes_scans.py    # /api/scans/* endpoints
│   │   └── routes_settings.py # /api/settings/* endpoints
│   ├── services/
│   │   ├── mounts.py          # Mount discovery from /proc/mounts
│   │   ├── scanner.py         # Core streaming scanner (batch SQLite inserts)
│   │   └── analytics.py       # Query helpers + user attribution cache
│   ├── web/
│   │   └── routes_pages.py    # Jinja2 page routes
│   ├── templates/             # HTML templates (Jinja2)
│   └── static/                # CSS
├── docs/                      # Detailed documentation
├── deploy/
│   ├── diskscan.service
│   └── diskscan.timer
├── tests/
│   ├── test_mount_detection.py
│   └── test_analytics.py
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```
