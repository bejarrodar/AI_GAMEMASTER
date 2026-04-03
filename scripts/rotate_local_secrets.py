from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from aigm.db.models import AdminAuditLog
from aigm.db.session import SessionLocal


def set_dotenv_value(path: Path, key: str, value: str) -> None:
    if not path.exists():
        path.write_text("", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate locally managed secrets in .env for development/self-hosting.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--rotate-admin-token", action="store_true")
    parser.add_argument("--rotate-backup-passphrase", action="store_true")
    parser.add_argument("--print-values", action="store_true", help="Print generated values (use carefully).")
    parser.add_argument("--audit-db", action="store_true", help="Write rotation audit event to admin_audit_logs.")
    parser.add_argument("--actor-id", default="local-operator")
    parser.add_argument("--actor-display", default="local-operator")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    rotated: dict[str, str] = {}
    if args.rotate_admin_token:
        rotated["AIGM_SYS_ADMIN_TOKEN"] = secrets.token_urlsafe(48)
    if args.rotate_backup_passphrase:
        rotated["AIGM_BACKUP_ENCRYPTION_PASSPHRASE"] = secrets.token_urlsafe(48)
    if not rotated:
        raise RuntimeError("No rotation selected. Use --rotate-admin-token and/or --rotate-backup-passphrase.")

    for key, value in rotated.items():
        set_dotenv_value(env_path, key, value)

    if args.audit_db:
        try:
            with SessionLocal() as db:
                db.add(
                    AdminAuditLog(
                        actor_source="script",
                        actor_id=args.actor_id.strip() or "local-operator",
                        actor_display=args.actor_display.strip() or "local-operator",
                        action="secret_rotated_local",
                        target=str(env_path),
                        audit_metadata={"keys": sorted(rotated.keys())},
                    )
                )
                db.commit()
        except Exception as exc:
            print(f"[rotate] warning: failed to write DB audit record: {exc}")

    print(f"[rotate] updated {len(rotated)} secrets in {env_path}")
    if args.print_values:
        for k, v in rotated.items():
            print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
