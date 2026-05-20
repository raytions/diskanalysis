# DiskAnalysis

A self-hosted disk analysis web panel for Linux servers with many NAS mount points.

**Architecture:** host-side scanner (Python CLI) + web panel (FastAPI + Jinja2, Docker).

---

## Quick Start

### 1. Install scanner dependencies on the host

```bash
cd /opt/diskanalysis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set DB_PATH to a path both the scanner and the web container can reach
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

# Scan a specific path, 180-day cold threshold, top 200 results
python -m app.scanner scan --path /mnt/nas1/projectA --cold-days 180 --top-n 200

# Exclude paths or globs
python -m app.scanner scan --mount /mnt/nas1 --exclude /mnt/nas1/.trash,*.tmp
```

### 3. Start the web panel

```bash
# Copy env and start
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
| `DEFAULT_COLD_DAYS` | `90` | Default cold-data threshold |
| `DEFAULT_TOP_N` | `100` | Default result count |
| `EXCLUDE_PATHS` | `/proc,/sys,/dev,/run,/snap` | Always-excluded paths |
| `NETWORK_FS_TYPES` | `nfs,nfs4,cifs,smb3,...` | FS types treated as network mounts |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `PATH_USER_ATTRIBUTION` | `true` | Attribute files by top-level directory name first (before uid). Useful for NFS4 mounts without uid isolation — e.g. `/media/NAS02/jiahao/` always goes to `jiahao` regardless of the file's uid. Set to `false` to use uid-only attribution. |

---

## Scheduled Scanning (systemd)

```bash
# Copy files
sudo cp deploy/diskscan.service /etc/systemd/system/
sudo cp deploy/diskscan.timer /etc/systemd/system/

# Edit WorkingDirectory and DB_PATH in diskscan.service
sudo systemctl daemon-reload
sudo systemctl enable --now diskscan.timer

# Check status
sudo systemctl status diskscan.timer
sudo journalctl -u diskscan.service -f
```

### Cron alternative

```cron
# /etc/cron.d/diskanalysis
0 2 * * *   root  cd /opt/diskanalysis && .venv/bin/python -m app.scanner scan --all-mounts
0 1 * * 0   root  cd /opt/diskanalysis && .venv/bin/python -m app.scanner scan --mount /mnt/nas1 --cold-days 90
```

---

## Reverse Proxy (Nginx example)

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

Mount the host filesystem read-only:

```yaml
volumes:
  - /:/host:ro
  - /proc:/proc:ro
```

And set `SCAN_ROOTS=/host/mnt` in `.env`. Be aware that `shutil.disk_usage` and `/proc/mounts` paths will differ inside the container.

### NAS mount scanning risks

- **Stale mounts** (especially NFS): `df` or `os.stat` on a stale NFS mount can block for minutes. The scanner uses a non-blocking `shutil.disk_usage` call but errors are caught and logged — a stale mount will show as inaccessible.
- **noatime / relatime**: Most modern Linux systems mount with `relatime` or `noatime`, meaning `atime` is not updated on every file read. **Cold file data should be treated as a best-effort indicator, not an absolute fact.** Files may appear "cold" even if they were recently read.
- **Permission errors**: Network filesystems (CIFS/NFS) may deny access even to root depending on server configuration. Permission errors are logged per-path and do not abort the scan.

### Why `--one-filesystem` matters

Without xdev (cross-device) protection, scanning `/mnt` on a server with 30 NAS mounts would recursively descend into all of them. The `--one-filesystem` flag (default: on) uses `os.stat().st_dev` to detect when a directory crosses into a different filesystem and skips it. Always leave this on when scanning NAS environments.

---

## Documentation

Detailed documentation is available in the [`docs/`](docs/) directory:

- [Architecture](docs/architecture.md) — system design, data flow, why scanner runs on host
- [Configuration](docs/configuration.md) — full `.env` reference
- [Scanner CLI](docs/scanner.md) — all scanner commands and flags
- [Deployment](docs/deployment.md) — Docker, systemd, nginx, reverse proxy
- [User Attribution](docs/user-attribution.md) — how per-user disk usage is calculated, NFS4 uid collision fix

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
│   ├── main.py              # FastAPI app
│   ├── config.py            # Settings (pydantic-settings)
│   ├── db.py                # SQLAlchemy engine + session
│   ├── models.py            # ORM models
│   ├── schemas.py           # Pydantic API schemas
│   ├── scanner.py           # CLI entry point
│   ├── api/
│   │   ├── routes_mounts.py # /api/mounts/* endpoints
│   │   └── routes_scans.py  # /api/scans/* endpoints
│   ├── services/
│   │   ├── mounts.py        # Mount discovery
│   │   ├── scanner.py       # Core scanner (streaming, batch-insert)
│   │   └── analytics.py     # Query helpers (largest files, cold data)
│   ├── web/
│   │   └── routes_pages.py  # Jinja2 page routes
│   ├── templates/           # HTML templates
│   └── static/              # CSS
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
