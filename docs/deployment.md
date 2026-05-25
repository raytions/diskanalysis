# Deployment Guide

## Docker (recommended for web panel)

```bash
# 1. Copy and edit config
cp .env.example .env
# Edit .env: set DB_PATH to a path reachable by both scanner and container

# 2. Create data directory
mkdir -p data

# 3. Start web panel
docker compose up -d

# 4. Open browser
open http://localhost:8000
```

The `docker-compose.yml` bind-mounts `./data` as `/data` inside the container. Set `DB_PATH=/data/diskanalysis.db` to share the database between host scanner and container.

## Systemd timer (for scanner)

```bash
# Copy unit files
sudo cp deploy/diskscan.service /etc/systemd/system/
sudo cp deploy/diskscan.timer /etc/systemd/system/

# Edit the service file — set WorkingDirectory and DB_PATH
sudo nano /etc/systemd/system/diskscan.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now diskscan.timer

# Check status
sudo systemctl status diskscan.timer
sudo journalctl -u diskscan.service -f
```

Example `diskscan.service`:

```ini
[Unit]
Description=DiskAnalysis Scanner

[Service]
Type=oneshot
WorkingDirectory=/opt/diskanalysis
Environment=DB_PATH=/opt/diskanalysis/data/diskanalysis.db
ExecStart=/opt/diskanalysis/.venv/bin/python -m app.scanner scan --all-mounts --cold-days 90
```

## Cron alternative

```cron
# /etc/cron.d/diskanalysis
# Full scan nightly at 02:00
0 2 * * *   root  cd /opt/diskanalysis && .venv/bin/python -m app.scanner scan --all-mounts

# Incremental scan of a specific NAS every 6 hours
0 */6 * * * root  cd /opt/diskanalysis && .venv/bin/python -m app.scanner scan --mount /media/nas02 --incremental
```

## Nginx reverse proxy

```nginx
server {
    listen 80;
    server_name diskanalysis.internal;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 10m;
    }
}
```

For HTTPS, add Certbot or point to your internal CA.

## Running without Docker

If you prefer to run the web panel directly on the host:

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env

# Start web panel
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Or with auto-reload for development
uvicorn app.main:app --reload --port 8000
```

## Auth setup

To enable admin authentication, set `ADMIN_PASSWORD` in `.env`:

```env
ADMIN_PASSWORD=your-strong-password-here
```

When set, all write operations (starting/stopping scans, deleting data, adding/removing mounts, modifying settings and users) require login. The dashboard and analytics pages are still publicly visible.

The admin user icon in the top-right nav bar shows login/logout options when auth is enabled.

## Triggering scans from the web UI

Scans can be started directly from the web panel without running the CLI:

- **Run Scan page** (`/trigger`): choose mode (selected mounts, all mounts, or specific path), scan type (full or incremental), cold threshold, and exclude rules.
- **Mount Points page**: right-click any mount → **Rescan**, or select multiple mounts and click **Scan** in the selection bar.
- **Mount Detail page**: click **Scan This Mount**.

Web-triggered scans run in background threads inside the web container. The scan detail page polls for live progress (files indexed, total size, elapsed time). Scans can be stopped mid-run via the **Stop** button.

## Containerising the scanner (advanced)

Not recommended — prefer running the scanner on the host. If you must containerise it, mount the host filesystem read-only:

```yaml
volumes:
  - /:/host:ro
  - /proc:/proc:ro
```

Set `SCAN_ROOTS=/host` and note that `shutil.disk_usage` paths and `/proc/mounts` entries will differ inside the container.
