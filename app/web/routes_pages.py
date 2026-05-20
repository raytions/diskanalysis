import logging
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Mount, Scan
from app.services import analytics
from app.config import settings
from app import auth

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} EB"


def _fmt_duration(s: float | None) -> str:
    if s is None:
        return "—"
    if s >= 3600:
        return f"{s/3600:.1f}h"
    if s >= 60:
        return f"{s/60:.1f}m"
    return f"{s:.1f}s"


templates.env.filters["fmt_bytes"] = _fmt_bytes
templates.env.filters["fmt_duration"] = _fmt_duration


def _tr(request: Request, name: str, context: dict):
    """Starlette 1.0 TemplateResponse: injects is_admin and auth_enabled."""
    context["is_admin"] = auth.is_admin_session(request.cookies.get("admin_token"))
    context["auth_enabled"] = auth.auth_enabled()
    return templates.TemplateResponse(request, name, context)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    summary = analytics.get_dashboard_summary(db)
    return _tr(request, "dashboard.html", {"summary": summary})


@router.get("/mounts", response_class=HTMLResponse)
def mounts_page(
    request: Request,
    fs_type: str = "",
    is_network: str = "",
    db: Session = Depends(get_db),
):
    q = db.query(Mount).filter(Mount.is_accessible == True)
    if fs_type:
        q = q.filter(Mount.fs_type == fs_type)
    if is_network == "yes":
        q = q.filter(Mount.is_network_fs == True)
    elif is_network == "no":
        q = q.filter(Mount.is_network_fs == False)
    mounts = q.order_by(Mount.usage_percent.desc()).all()

    # Lazy backfill: populate last_scanned_at for mounts that were scanned before this
    # field was reliably updated (e.g. via selected_mounts scans with mark_completed=False)
    from app.services.analytics import _latest_scan_id_for_mount
    updated = False
    for mount in mounts:
        if mount.last_scanned_at is None:
            sid = _latest_scan_id_for_mount(db, mount.id)
            if sid:
                s = db.get(Scan, sid)
                if s and s.finished_at:
                    mount.last_scanned_at = s.finished_at
                    updated = True
    if updated:
        db.commit()

    all_fs_types = sorted({m.fs_type for m in db.query(Mount).filter(Mount.is_accessible == True).all()})
    return _tr(request, "mounts.html", {
        "mounts": mounts,
        "all_fs_types": all_fs_types,
        "fs_type": fs_type,
        "is_network": is_network,
    })


@router.get("/mounts/{mount_id}", response_class=HTMLResponse)
def mount_detail(
    request: Request,
    mount_id: int,
    cold_days: int = 90,
    top_n: int = 50,
    db: Session = Depends(get_db),
):
    mount = db.query(Mount).get(mount_id)
    if not mount:
        return HTMLResponse("Mount not found", status_code=404)

    from app.services.analytics import _latest_scan_id_for_mount
    latest_scan_id = _latest_scan_id_for_mount(db, mount_id)
    latest_scan = db.query(Scan).get(latest_scan_id) if latest_scan_id else None

    return _tr(request, "mount_detail.html", {
        "mount": mount,
        "cold_days": cold_days,
        "top_n": top_n,
        "total_files": latest_scan.total_files if latest_scan else None,
        "total_dirs": latest_scan.total_dirs if latest_scan else None,
    })


@router.get("/scans", response_class=HTMLResponse)
def scans_page(request: Request, db: Session = Depends(get_db)):
    scans = db.query(Scan).order_by(Scan.started_at.desc()).limit(100).all()
    mounts = db.query(Mount).filter(Mount.is_accessible == True).order_by(Mount.mount_point).all()
    return _tr(request, "scans.html", {
        "scans": scans,
        "mounts": mounts,
        "default_cold_days": settings.DEFAULT_COLD_DAYS,
    })


@router.get("/scans/{scan_id}", response_class=HTMLResponse)
def scan_detail_page(request: Request, scan_id: int, db: Session = Depends(get_db)):
    scan = db.query(Scan).get(scan_id)
    if not scan:
        return HTMLResponse("Scan not found", status_code=404)
    from app.models import ScanError
    errors = db.query(ScanError).filter(ScanError.scan_id == scan_id).limit(200).all()
    return _tr(request, "scan_detail.html", {"scan": scan, "errors": errors})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import text
    from app.services.analytics import get_path_attribution_enabled
    with db.bind.connect() as conn:
        path_attr = get_path_attribution_enabled(conn)
        users = conn.execute(text(
            "SELECT username, created_at FROM custom_users ORDER BY username"
        )).fetchall()
    return _tr(request, "settings.html", {
        "path_user_attribution": path_attr,
        "users": [{"username": r[0], "created_at": r[1]} for r in users],
    })


@router.get("/trigger", response_class=HTMLResponse)
def trigger_page(request: Request):
    return RedirectResponse("/scans", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if not auth.auth_enabled():
        return RedirectResponse("/", status_code=302)
    if auth.is_admin_session(request.cookies.get("admin_token")):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": error, "is_admin": False, "auth_enabled": True})


@router.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    if not auth.auth_enabled():
        return RedirectResponse("/", status_code=302)
    if auth.verify_password(password):
        token = auth.create_session()
        response = RedirectResponse("/", status_code=302)
        response.set_cookie("admin_token", token, httponly=True, max_age=86400 * 7, samesite="lax")
        return response
    return templates.TemplateResponse(request, "login.html", {
        "error": "密码错误", "is_admin": False, "auth_enabled": True
    }, status_code=401)


@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get("admin_token")
    if token:
        auth.revoke_session(token)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("admin_token")
    return response
