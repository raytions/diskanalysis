"""
Mount discovery service.

Reads /proc/mounts, detects filesystem types, measures disk usage,
and upserts mount records into the database.
"""
import logging
import os
import shutil
from datetime import datetime
from typing import Iterator

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Mount
from app.services.mount_events import log_mount_event

logger = logging.getLogger(__name__)

# Pseudo-filesystems that are never interesting for disk analysis
_SKIP_FS_TYPES = frozenset({
    "proc", "sysfs", "devpts", "devtmpfs", "tmpfs", "cgroup", "cgroup2",
    "pstore", "securityfs", "debugfs", "hugetlbfs", "mqueue", "fusectl",
    "binfmt_misc", "autofs", "configfs", "efivarfs", "overlay",
    "squashfs",   # snap packages
    "bpf",        # /sys/fs/bpf
    "tracefs",    # /sys/kernel/tracing
    "rpc_pipefs", # /run/rpc_pipefs (NFS helper)
    "nsfs",       # Docker/snap network namespaces
    "fuse.portal",       # /run/user/*/doc (xdg-document-portal)
    "fuse.gvfsd-fuse",   # /run/user/*/gvfs (GNOME VFS)
    "ramfs", "pipefs", "sockfs", "anon_inodefs",
})

# Pseudo-devices that are never real block devices
_SKIP_DEVICES = frozenset({
    "none", "udev", "tmpfs", "cgroup", "systemd-1",
    "bpf", "tracefs", "rpc_pipefs", "nsfs", "sunrpc",
})

# Path prefixes to always skip (beyond EXCLUDE_PATHS setting)
_SKIP_PATH_PREFIXES = (
    "/sys/",
    "/run/",
    "/boot/",
    "/snap/",
    "/proc/",
    "/dev/",
)


def _parse_proc_mounts() -> Iterator[dict]:
    """Yield raw dicts from /proc/mounts."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                yield {
                    "device": _decode_proc_mount_field(parts[0]),
                    "mount_point": _decode_proc_mount_field(parts[1]),
                    "fs_type": parts[2],
                    "options": parts[3],
                }
    except OSError as e:
        logger.error("Cannot read /proc/mounts: %s", e)


def _is_network_fs(fs_type: str) -> bool:
    return fs_type.lower() in settings.network_fs_types_set


def _decode_proc_mount_field(value: str) -> str:
    try:
        return bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return value


def _get_disk_usage(mount_point: str) -> tuple[int, int, int, float]:
    """Return (total, used, free, percent). Returns zeros on error."""
    try:
        usage = shutil.disk_usage(mount_point)
        percent = (usage.used / usage.total * 100) if usage.total else 0.0
        return usage.total, usage.used, usage.free, round(percent, 2)
    except OSError as e:
        logger.warning("disk_usage(%s) failed: %s", mount_point, e)
        return 0, 0, 0, 0.0


def discover_mounts() -> list[dict]:
    """
    Return a list of mount-point dicts for interesting filesystems.
    Filters out pseudo-fs, applies EXCLUDE_PATHS, respects ALLOWED_FS_TYPES.
    """
    exclude = set(settings.exclude_paths_list)
    allowed = settings.allowed_fs_types_set  # empty = allow all real fs

    results: list[dict] = []
    seen: set[str] = set()

    for raw in _parse_proc_mounts():
        mp = raw["mount_point"]
        fs = raw["fs_type"].lower()
        device = raw["device"]

        if mp in seen:
            continue
        seen.add(mp)

        if fs in _SKIP_FS_TYPES:
            continue
        if device in _SKIP_DEVICES and not _is_network_fs(fs):
            continue
        # Prefix-match excludes (e.g. "/sys" covers "/sys/fs/bpf")
        if any(mp == ex or mp.startswith(ex.rstrip("/") + "/") for ex in exclude):
            continue
        if allowed and fs not in allowed:
            continue
        # Always skip known system path prefixes
        if any(mp.startswith(pfx) for pfx in _SKIP_PATH_PREFIXES):
            continue

        is_net = _is_network_fs(fs)
        total, used, free, pct = _get_disk_usage(mp)
        accessible = os.path.isdir(mp) and os.access(mp, os.R_OK)

        results.append({
            "mount_point": mp,
            "device": device,
            "fs_type": fs,
            "is_network_fs": is_net,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "usage_percent": pct,
            "is_accessible": accessible,
        })

    return results


def refresh_mounts(db: Session) -> list[Mount]:
    """
    Discover current mounts and upsert them into the database.
    Returns the list of Mount ORM objects.
    """
    discovered = discover_mounts()
    now = datetime.now()
    mounts: list[Mount] = []

    for info in discovered:
        mp = info["mount_point"]
        mount = db.query(Mount).filter(Mount.mount_point == mp).first()
        if mount is None:
            mount = Mount(mount_point=mp, created_at=now)
            db.add(mount)
            previous_accessible = None
        else:
            previous_accessible = mount.is_accessible

        mount.device = info["device"]
        mount.fs_type = info["fs_type"]
        mount.is_network_fs = info["is_network_fs"]
        mount.total_bytes = info["total_bytes"]
        mount.used_bytes = info["used_bytes"]
        mount.free_bytes = info["free_bytes"]
        mount.usage_percent = info["usage_percent"]
        mount.is_accessible = info["is_accessible"]
        mount.updated_at = now
        mounts.append(mount)

        if previous_accessible is False and info["is_accessible"]:
            try:
                log_mount_event(
                    db,
                    mount_id=mount.id,
                    mount_point=mp,
                    event_type="state",
                    status="ok",
                    message="Mount became accessible during refresh.",
                    details={"fs_type": info["fs_type"], "device": info["device"]},
                )
            except Exception as e:
                logger.debug("Failed to log mount reconnect event for %s: %s", mp, e)

    # Mark any previously-seen mount that's no longer discovered as inaccessible
    current_mps = {info["mount_point"] for info in discovered}
    stale = db.query(Mount).filter(Mount.mount_point.notin_(current_mps)).all()
    for m in stale:
        was_accessible = m.is_accessible
        m.is_accessible = False
        m.updated_at = now
        if was_accessible:
            try:
                log_mount_event(
                    db,
                    mount_id=m.id,
                    mount_point=m.mount_point,
                    event_type="state",
                    status="warning",
                    message="Mount disappeared from /proc/mounts during refresh.",
                    details={"fs_type": m.fs_type, "device": m.device},
                )
            except Exception as e:
                logger.debug("Failed to log stale mount event for %s: %s", m.mount_point, e)

    db.commit()
    for m in mounts:
        db.refresh(m)
    logger.info("Refreshed %d mounts, %d stale", len(mounts), len(stale))
    return mounts
