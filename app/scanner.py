"""
Host-side scanner CLI.

Usage:
  python -m app.scanner mounts
  python -m app.scanner doctor
  python -m app.scanner scan --all-mounts
  python -m app.scanner scan --mount /mnt/nas1
  python -m app.scanner scan --path /mnt/nas1/projectA
  python -m app.scanner scan --mount /mnt/nas1 --cold-days 90 --top-n 200 --exclude /tmp,/cache
"""
import argparse
import logging
import os
import sys
from datetime import datetime

# Make sure the project root is in path when running as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("scanner")


def cmd_mounts(args) -> None:
    from app.services.mounts import discover_mounts
    mounts = discover_mounts()
    if not mounts:
        print("No mounts discovered.")
        return
    print(f"{'Mount Point':<30} {'Device':<25} {'FS':<10} {'Net':<5} {'Used%':<7} {'Total':<12} {'Free':<12}")
    print("-" * 105)
    for m in mounts:
        net = "yes" if m["is_network_fs"] else "no"
        total_gb = m["total_bytes"] / (1024 ** 3)
        free_gb = m["free_bytes"] / (1024 ** 3)
        print(
            f"{m['mount_point']:<30} {m['device']:<25} {m['fs_type']:<10} "
            f"{net:<5} {m['usage_percent']:>5.1f}% {total_gb:>8.1f} GB  {free_gb:>8.1f} GB"
        )
    print(f"\nTotal: {len(mounts)} mounts")


def cmd_doctor(args) -> None:
    from app.db import init_db, engine
    from app.services.mounts import discover_mounts
    import sqlalchemy

    print("=== DiskAnalysis Doctor ===\n")

    # DB check
    try:
        init_db()
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        print(f"[OK] Database: {settings.DB_PATH}")
    except Exception as e:
        print(f"[FAIL] Database: {e}")

    # Mount check
    try:
        mounts = discover_mounts()
        accessible = [m for m in mounts if m["is_accessible"]]
        inaccessible = [m for m in mounts if not m["is_accessible"]]
        print(f"[OK] Mounts discovered: {len(mounts)} ({len(accessible)} accessible, {len(inaccessible)} inaccessible)")
        for m in inaccessible:
            print(f"  [WARN] {m['mount_point']} — not accessible")
    except Exception as e:
        print(f"[FAIL] Mount discovery: {e}")

    # Config check
    print(f"[OK] DB_PATH       = {settings.DB_PATH}")
    print(f"[OK] EXCLUDE_PATHS = {settings.EXCLUDE_PATHS}")
    print(f"[OK] NETWORK_FS    = {settings.NETWORK_FS_TYPES}")
    print(f"[OK] LOG_LEVEL     = {settings.LOG_LEVEL}")
    print("\nDoctor complete.")


def cmd_scan(args) -> None:
    from app.db import SessionLocal, init_db
    from app.models import Mount, Scan
    from app.services import mounts as mount_svc
    from app.services.scanner import run_scan

    init_db()
    db = SessionLocal()

    exclude_paths = [e.strip() for e in args.exclude.split(",") if e.strip()] if args.exclude else []

    try:
        if args.all_mounts:
            print("Refreshing mount list…")
            mounts = mount_svc.refresh_mounts(db)
            accessible = [m for m in mounts if m.is_accessible]
            print(f"Found {len(accessible)} accessible mount(s). Starting scan…\n")

            for mount in accessible:
                print(f"  Scanning {mount.mount_point} ({mount.fs_type})…", flush=True)
                scan = Scan(
                    mount_id=mount.id,
                    started_at=datetime.utcnow(),
                    status="running",
                    scan_mode="all_mounts",
                    target_path=mount.mount_point,
                    filesystem_boundary_only=args.one_filesystem,
                    exclude_rules=args.exclude,
                    cold_days=args.cold_days,
                    top_n=args.top_n,
                )
                db.add(scan)
                db.commit()
                db.refresh(scan)
                run_scan(db, scan, mount.mount_point, args.one_filesystem, exclude_paths)
                print(f"  Done: {scan.total_files} files, {_fmt(scan.total_bytes)}, {scan.error_count} errors, {scan.duration_seconds:.1f}s")

        elif args.mount:
            mp = args.mount.rstrip("/")
            mount_svc.refresh_mounts(db)
            mount = db.query(Mount).filter(Mount.mount_point == mp).first()
            if not mount:
                print(f"ERROR: Mount not found in DB: {mp}")
                print("Run `python -m app.scanner mounts` to see available mounts.")
                sys.exit(1)
            print(f"Scanning {mp}…")
            scan = Scan(
                mount_id=mount.id,
                started_at=datetime.utcnow(),
                status="running",
                scan_mode="single_mount",
                target_path=mp,
                filesystem_boundary_only=args.one_filesystem,
                exclude_rules=args.exclude,
                cold_days=args.cold_days,
                top_n=args.top_n,
            )
            db.add(scan)
            db.commit()
            db.refresh(scan)
            run_scan(db, scan, mp, args.one_filesystem, exclude_paths)
            print(f"Done: {scan.total_files} files, {_fmt(scan.total_bytes)}, {scan.error_count} errors, {scan.duration_seconds:.1f}s")

        elif args.path:
            print(f"Scanning path: {args.path}")
            scan = Scan(
                mount_id=None,
                started_at=datetime.utcnow(),
                status="running",
                scan_mode="path",
                target_path=args.path,
                filesystem_boundary_only=args.one_filesystem,
                exclude_rules=args.exclude,
                cold_days=args.cold_days,
                top_n=args.top_n,
            )
            db.add(scan)
            db.commit()
            db.refresh(scan)
            run_scan(db, scan, args.path, args.one_filesystem, exclude_paths)
            print(f"Done: {scan.total_files} files, {_fmt(scan.total_bytes)}, {scan.error_count} errors, {scan.duration_seconds:.1f}s")

        else:
            print("Specify --all-mounts, --mount <path>, or --path <path>")
            sys.exit(1)

    finally:
        db.close()


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main():
    parser = argparse.ArgumentParser(prog="app.scanner", description="DiskAnalysis host-side scanner")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("mounts", help="List discovered mount points")
    sub.add_parser("doctor", help="Check configuration and database health")

    p_scan = sub.add_parser("scan", help="Run a directory scan")
    p_scan.add_argument("--all-mounts", action="store_true", help="Scan all accessible mounts")
    p_scan.add_argument("--mount", metavar="PATH", help="Scan a single mount point")
    p_scan.add_argument("--path", metavar="PATH", help="Scan any directory path")
    p_scan.add_argument("--cold-days", type=int, default=settings.DEFAULT_COLD_DAYS, metavar="N",
                        help=f"Cold-data threshold in days (default: {settings.DEFAULT_COLD_DAYS})")
    p_scan.add_argument("--top-n", type=int, default=settings.DEFAULT_TOP_N, metavar="N",
                        help=f"Top-N results (default: {settings.DEFAULT_TOP_N})")
    p_scan.add_argument("--exclude", metavar="PATHS", default=None,
                        help="Comma-separated paths/globs to exclude (e.g. /tmp,*.cache)")
    p_scan.add_argument("--one-filesystem", action="store_true", default=True,
                        help="Stay within a single filesystem (recommended, default: on)")
    p_scan.add_argument("--follow-mounts", action="store_false", dest="one_filesystem",
                        help="Allow crossing filesystem boundaries (dangerous with NAS)")

    args = parser.parse_args()

    if args.command == "mounts":
        cmd_mounts(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "scan":
        cmd_scan(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
