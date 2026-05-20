import logging
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _enable_wal(dbapi_conn, connection_record):
    import os.path as _osp
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-64000")
    cursor.execute("PRAGMA busy_timeout=15000")   # wait up to 15 s on lock contention
    cursor.close()
    # Register Python-backed SQL functions
    dbapi_conn.create_function("basename", 1, _osp.basename)


def create_db_engine():
    Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        settings.db_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    event.listen(engine, "connect", _enable_wal)
    return engine


engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401 – registers all ORM classes
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
    logger.info("Database initialised at %s", settings.DB_PATH)


def _migrate(eng):
    """Apply any additive schema changes not handled by create_all."""
    from sqlalchemy import text
    with eng.connect() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(files)"))}
        if "uid" not in existing:
            conn.execute(text("ALTER TABLE files ADD COLUMN uid INTEGER"))
            conn.commit()
            logger.info("Migrated: added files.uid column")
        # Add composite index for fast mount→scan lookup (used by _latest_scan_id_for_mount)
        existing_idx = {row[1] for row in conn.execute(text("PRAGMA index_list(files)"))}
        if "ix_files_mount_scan" not in existing_idx:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_files_mount_scan ON files (mount_id, scan_id)"))
            conn.commit()
            logger.info("Migrated: added ix_files_mount_scan index")

        # Add covering index for cold-file aggregation (speeds up dashboard cold stats)
        if "ix_files_scan_cold_size" not in existing_idx:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_files_scan_cold_size ON files (scan_id, is_cold, size_bytes)"))
            conn.commit()
            logger.info("Migrated: added ix_files_scan_cold_size index")

        # Add cold stats columns to scans table
        scan_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(scans)"))}
        if "cold_file_count" not in scan_cols:
            conn.execute(text("ALTER TABLE scans ADD COLUMN cold_file_count INTEGER DEFAULT 0"))
            conn.commit()
        if "cold_total_size_bytes" not in scan_cols:
            conn.execute(text("ALTER TABLE scans ADD COLUMN cold_total_size_bytes INTEGER DEFAULT 0"))
            conn.commit()
            logger.info("Migrated: added scans cold stats columns")

        # Partial covering index for largest-files queries: (mount_id, scan_id, size_bytes DESC)
        # only on non-symlinks — allows ORDER BY size_bytes DESC LIMIT N without a full sort
        if "ix_files_ms_size" not in existing_idx:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_files_ms_size "
                "ON files (mount_id, scan_id, size_bytes DESC) WHERE is_symlink=0"
            ))
            conn.commit()
            logger.info("Migrated: added ix_files_ms_size index")

        # Covering index for cold-files-by-size per mount: enables instant ORDER BY LIMIT for cold tab
        if "ix_files_ms_cold_size" not in existing_idx:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_files_ms_cold_size "
                "ON files (mount_id, scan_id, is_cold, size_bytes DESC) WHERE is_symlink=0"
            ))
            conn.commit()
            logger.info("Migrated: added ix_files_ms_cold_size index")

        # Partial covering index for atime (cold files) ordering
        if "ix_files_ms_atime" not in existing_idx:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_files_ms_atime "
                "ON files (mount_id, scan_id, atime) WHERE is_symlink=0"
            ))
            conn.commit()
            logger.info("Migrated: added ix_files_ms_atime index")

        # Partial covering index for mtime ordering
        if "ix_files_ms_mtime" not in existing_idx:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_files_ms_mtime "
                "ON files (mount_id, scan_id, mtime) WHERE is_symlink=0"
            ))
            conn.commit()
            logger.info("Migrated: added ix_files_ms_mtime index")

        # username column for multi-server user grouping
        if "username" not in existing:
            conn.execute(text("ALTER TABLE files ADD COLUMN username TEXT"))
            conn.commit()
            logger.info("Migrated: added files.username column")

        # Re-read index list after any new indexes above may have been created
        existing_idx = {row[1] for row in conn.execute(text("PRAGMA index_list(files)"))}

        if "ix_files_scan_username" not in existing_idx:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_files_scan_username ON files (scan_id, username)"))
            conn.commit()
            logger.info("Migrated: added ix_files_scan_username index")

        if "ix_files_scan_uid_size" not in existing_idx:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_files_scan_uid_size ON files (scan_id, uid, size_bytes)"))
            conn.commit()
            logger.info("Migrated: added ix_files_scan_uid_size index")

        dir_idx = {row[1] for row in conn.execute(text("PRAGMA index_list(directories)"))}
        if "ix_dirs_scan_total_size" not in dir_idx:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dirs_scan_total_size ON directories (scan_id, total_size_bytes)"))
            conn.commit()
            logger.info("Migrated: added ix_dirs_scan_total_size index")
        if "ix_dirs_mount_scan_size" not in dir_idx:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_dirs_mount_scan_size ON directories (mount_id, scan_id, total_size_bytes DESC)"))
            conn.commit()
            logger.info("Migrated: added ix_dirs_mount_scan_size index")

        # Custom user list for path-based attribution
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS custom_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.commit()

        # App-level settings (key-value)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """))
        conn.commit()
