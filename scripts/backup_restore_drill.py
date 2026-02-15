from __future__ import annotations

import argparse
import sqlite3
import shutil
from pathlib import Path

from aigm.config import settings
from aigm.ops.backup_crypto import decrypt_file, encrypt_file


def _sqlite_path_from_url(db_url: str) -> Path:
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        raise ValueError("This drill currently supports sqlite URLs only.")
    return Path(db_url[len(prefix) :]).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run sqlite backup/restore drill with optional encryption.")
    parser.add_argument("--workdir", default="backups/drill", help="Working folder for drill artifacts.")
    parser.add_argument("--passphrase", default="", help="Optional passphrase for encrypted drill.")
    args = parser.parse_args()

    if not settings.database_url.startswith("sqlite:///"):
        raise RuntimeError("backup_restore_drill currently supports sqlite only.")

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    backup_path = workdir / "drill_backup.db"
    restore_copy = workdir / "drill_restore_copy.db"

    src_db = _sqlite_path_from_url(settings.database_url)
    if not src_db.exists():
        raise FileNotFoundError(f"SQLite DB file not found: {src_db}")
    shutil.copy2(src_db, backup_path)
    if args.passphrase.strip():
        enc_path = workdir / "drill_backup.db.enc"
        decrypt_copy = workdir / "drill_backup.dec.db"
        encrypt_file(backup_path, enc_path, args.passphrase.strip())
        decrypt_file(enc_path, decrypt_copy, args.passphrase.strip())
        restore_source = decrypt_copy
    else:
        restore_source = backup_path

    restore_copy.write_bytes(restore_source.read_bytes())
    with sqlite3.connect(str(restore_copy)) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
        _ = cur.fetchone()
    print(f"[drill] backup/restore verification succeeded: {restore_copy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
