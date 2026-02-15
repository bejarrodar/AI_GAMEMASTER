from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from aigm.config import settings
from aigm.ops.backup_crypto import decrypt_file


def _normalize_pg_url(db_url: str) -> str:
    if db_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + db_url[len("postgresql+psycopg://") :]
    return db_url


def _sqlite_path_from_url(db_url: str) -> Path:
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        raise ValueError("Not a sqlite URL")
    raw = db_url[len(prefix) :]
    return Path(raw).resolve()


def restore_sqlite(backup_path: Path, force: bool) -> None:
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")
    target = _sqlite_path_from_url(settings.database_url)
    if target.exists() and not force:
        raise RuntimeError(
            f"Target DB exists: {target}. Use --force to overwrite (ensure services are stopped first)."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target)


def restore_postgres(backup_path: Path) -> None:
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")
    pg_url = _normalize_pg_url(settings.database_url)
    cmd = ["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", pg_url, str(backup_path)]
    subprocess.run(cmd, check=True)


def _effective_passphrase(cli_passphrase: str, cli_passphrase_file: str) -> str:
    if cli_passphrase:
        return cli_passphrase
    if cli_passphrase_file:
        return Path(cli_passphrase_file).read_text(encoding="utf-8").strip()
    if settings.backup_encryption_passphrase.strip():
        return settings.backup_encryption_passphrase.strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore configured AIGM database from backup.")
    parser.add_argument("backup_path", help="Backup file path (.db for sqlite, .dump for postgres custom format).")
    parser.add_argument("--force", action="store_true", help="Allow overwriting existing sqlite DB file.")
    parser.add_argument("--encrypted", action="store_true", help="Treat backup as encrypted AIGM payload.")
    parser.add_argument("--passphrase", default="", help="Passphrase for encrypted backup restore.")
    parser.add_argument("--passphrase-file", default="", help="Path to file containing passphrase.")
    args = parser.parse_args()

    backup_path = Path(args.backup_path)
    passphrase = _effective_passphrase(args.passphrase, args.passphrase_file)
    restore_path = backup_path
    temp_plain: Path | None = None
    if args.encrypted:
        if not passphrase:
            raise RuntimeError("Encrypted restore requested but no passphrase was provided.")
        temp_plain = Path(str(backup_path) + ".tmp_plain_restore")
        decrypt_file(backup_path, temp_plain, passphrase)
        restore_path = temp_plain
    if settings.database_url.startswith("sqlite:///"):
        restore_sqlite(restore_path, force=bool(args.force))
    elif settings.database_url.startswith("postgresql"):
        restore_postgres(restore_path)
    else:
        raise RuntimeError("Unsupported database URL scheme for restore.")
    if temp_plain is not None:
        temp_plain.unlink(missing_ok=True)

    print(f"[restore] restore completed from: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
