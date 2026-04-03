from __future__ import annotations

import argparse
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from aigm.config import settings
from aigm.ops.backup_crypto import encrypt_file


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


def backup_sqlite(out_path: Path) -> None:
    src = _sqlite_path_from_url(settings.database_url)
    if not src.exists():
        raise FileNotFoundError(f"SQLite DB file not found: {src}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out_path)


def backup_postgres(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pg_url = _normalize_pg_url(settings.database_url)
    cmd = ["pg_dump", "--format=custom", "--file", str(out_path), pg_url]
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
    parser = argparse.ArgumentParser(description="Backup configured AIGM database.")
    parser.add_argument("--output", default="", help="Output path. Defaults by DB type in ./backups.")
    parser.add_argument("--encrypt", action="store_true", help="Encrypt backup output using passphrase.")
    parser.add_argument("--passphrase", default="", help="Passphrase for backup encryption.")
    parser.add_argument("--passphrase-file", default="", help="Path to file containing passphrase.")
    args = parser.parse_args()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    passphrase = _effective_passphrase(args.passphrase, args.passphrase_file)
    if args.encrypt and not passphrase:
        raise RuntimeError("Encryption requested but no passphrase was provided.")
    if settings.database_url.startswith("sqlite:///"):
        default_out = Path("backups") / f"aigm_sqlite_{ts}.db"
        plain_out = Path(args.output) if args.output else default_out
        if args.encrypt and plain_out.suffix != ".enc":
            out_path = Path(str(plain_out) + ".enc")
        else:
            out_path = plain_out
        if args.encrypt:
            tmp_plain = Path(str(out_path) + ".tmp_plain")
            backup_sqlite(tmp_plain)
            encrypt_file(tmp_plain, out_path, passphrase)
            tmp_plain.unlink(missing_ok=True)
        else:
            backup_sqlite(out_path)
    elif settings.database_url.startswith("postgresql"):
        default_out = Path("backups") / f"aigm_postgres_{ts}.dump"
        plain_out = Path(args.output) if args.output else default_out
        if args.encrypt and plain_out.suffix != ".enc":
            out_path = Path(str(plain_out) + ".enc")
        else:
            out_path = plain_out
        if args.encrypt:
            tmp_plain = Path(str(out_path) + ".tmp_plain")
            backup_postgres(tmp_plain)
            encrypt_file(tmp_plain, out_path, passphrase)
            tmp_plain.unlink(missing_ok=True)
        else:
            backup_postgres(out_path)
    else:
        raise RuntimeError("Unsupported database URL scheme for backup.")

    print(f"[backup] wrote backup: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
