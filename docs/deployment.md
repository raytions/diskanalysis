# Deployment Guide

DiskAnalysis can run directly on a Linux host or in Docker. For the most accurate mount discovery and filesystem metadata, run it where it can see the real mount paths and `/proc/mounts`.

## Docker

```bash
cp .env.example .env
mkdir -p data
docker compose up -d
```

Open:

```text
http://localhost:8000
```

The compose file stores SQLite data in `./data` and exposes the app on `APP_PORT`.

If the container must scan host paths, bind-mount those paths explicitly:

```yaml
services:
  web:
    volumes:
      - ./data:/data
      - /mnt/research-a:/mnt/research-a:ro
      - /mnt/archive-b:/mnt/archive-b:ro
      - /proc:/proc:ro
```

Set:

```env
DB_PATH=/data/diskanalysis.db
```

## Host Mode

Host mode is often simpler for NAS-heavy servers.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
mkdir -p data

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For persistent service management, use systemd.

## systemd Web Service

Example:

```ini
[Unit]
Description=DiskAnalysis Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/diskanalysis
EnvironmentFile=/opt/diskanalysis/.env
ExecStart=/opt/diskanalysis/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now diskanalysis-web.service
sudo journalctl -u diskanalysis-web.service -f
```

## Scheduled Scans

Preferred option: configure **Auto Scan** from the Scans page.

Auto Scan stores its schedule in SQLite and shows:

- enabled/disabled state
- interval
- target selection
- next run time
- last started time
- last scheduler status message
- currently active scheduled scans

CLI cron/systemd is still possible if you prefer external scheduling:

```cron
# Full scan every night at 02:00
0 2 * * * root cd /opt/diskanalysis && .venv/bin/python -m app.scanner scan --all-mounts
```

The old incremental scan examples were removed because incremental scan mode is no longer supported.

## SSHFS Requirements

For SSHFS mount management from the web UI, install host packages:

```bash
sudo apt install sshfs fuse3 openssh-client sshpass
```

Notes:

- `sshfs` performs the actual FUSE mount.
- `fusermount3`/`fusermount` is used for unmount and lazy detach.
- `sshpass` is used only when a password is supplied through the web form.
- Key-based SSHFS mounts can use a configured private key path.

The app can create mount directories from the web UI. Normal mode uses the web process user. Sudo mkdir mode is intentionally narrow and only creates local mount directories.

## SSHFS Failure Handling

SSHFS can return success while the mount path is unreadable, commonly with `EIO`. DiskAnalysis verifies the mount point after `sshfs` exits.

If the path is not accessible, it:

1. records the failure
2. runs a remote-path diagnostic where possible
3. lazy-detaches the bad FUSE mount
4. keeps the event in Recent Mount Events

This prevents stale broken SSHFS mounts from showing as healthy.

## Nginx Reverse Proxy

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

For HTTPS, terminate TLS with Certbot or an internal CA.

## Authentication

Set an admin password:

```env
ADMIN_PASSWORD=change-this-password
```

When auth is enabled:

- public pages can still be viewed
- write operations require login
- admin-only UI controls are hidden until login

Write operations include starting/stopping scans, deleting scans, editing mounts, mounting/unmounting, changing settings, and user management.

## Health Check

The API exposes:

```text
GET /api/health
```

Use it for Docker or reverse-proxy checks.

## Public GitHub Checklist

Before publishing a public repository:

- review screenshots for private hostnames, user names, and mount names
- leave `.env` untracked
- keep only `.env.example`
- decide and add a license if you want open-source reuse
- remove private deployment notes from issues or docs
