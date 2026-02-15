from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, text

from aigm.config import settings
from aigm.db.models import AdminAuditLog, BotConfig, Campaign, Player, SystemLog, TurnLog
from aigm.db.session import SessionLocal


def _parse_bool(value: str | None, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


class DBAPIState:
    def __init__(self) -> None:
        self.token = settings.db_api_token.strip()

    def auth_ok(self, authorization: str) -> bool:
        if not self.token:
            return True
        return (authorization or "").strip() == f"Bearer {self.token}"

    @staticmethod
    def health() -> dict:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            return {"ok": True, "checks": {"db": {"ok": True, "detail": "reachable"}}}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "checks": {"db": {"ok": False, "detail": str(exc)}}}

    @staticmethod
    def list_bots(enabled: bool | None = None) -> list[dict]:
        with SessionLocal() as db:
            q = db.query(BotConfig)
            if enabled is not None:
                q = q.filter(BotConfig.is_enabled.is_(enabled))
            rows = q.order_by(BotConfig.id.asc()).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "discord_token": row.discord_token,
                "is_enabled": bool(row.is_enabled),
                "notes": row.notes or "",
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def create_bot(payload: dict) -> dict:
        name = str(payload.get("name", "")).strip()
        token = str(payload.get("discord_token", "")).strip()
        if not name or not token:
            raise ValueError("name and discord_token are required")
        with SessionLocal() as db:
            if db.query(BotConfig).filter(BotConfig.name == name).one_or_none():
                raise ValueError("bot config name already exists")
            row = BotConfig(
                name=name,
                discord_token=token,
                is_enabled=bool(payload.get("is_enabled", True)),
                notes=str(payload.get("notes", "") or ""),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        return {"id": row.id, "name": row.name}

    @staticmethod
    def update_bot(bot_id: int, payload: dict) -> dict:
        with SessionLocal() as db:
            row = db.query(BotConfig).filter(BotConfig.id == int(bot_id)).one_or_none()
            if row is None:
                raise ValueError("bot config not found")
            if "name" in payload and str(payload["name"]).strip():
                row.name = str(payload["name"]).strip()
            if "discord_token" in payload and str(payload["discord_token"]).strip():
                row.discord_token = str(payload["discord_token"]).strip()
            if "is_enabled" in payload:
                row.is_enabled = bool(payload["is_enabled"])
            if "notes" in payload:
                row.notes = str(payload.get("notes") or "")
            row.updated_at = datetime.utcnow()
            db.add(row)
            db.commit()
            db.refresh(row)
        return {"id": row.id, "name": row.name}

    @staticmethod
    def delete_bot(bot_id: int) -> dict:
        with SessionLocal() as db:
            row = db.query(BotConfig).filter(BotConfig.id == int(bot_id)).one_or_none()
            if row is None:
                raise ValueError("bot config not found")
            db.delete(row)
            db.commit()
        return {"id": int(bot_id), "deleted": True}

    @staticmethod
    def list_system_logs(limit: int, service: str, level: str) -> list[dict]:
        with SessionLocal() as db:
            q = db.query(SystemLog)
            if service:
                q = q.filter(SystemLog.service == service)
            if level:
                q = q.filter(SystemLog.level == level)
            rows = q.order_by(SystemLog.id.desc()).limit(max(1, min(500, int(limit)))).all()
        return [
            {
                "id": row.id,
                "service": row.service,
                "level": row.level,
                "message": row.message,
                "source": row.source,
                "metadata": row.log_metadata,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def list_audit_logs(limit: int) -> list[dict]:
        with SessionLocal() as db:
            rows = db.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(max(1, min(500, int(limit)))).all()
        return [
            {
                "id": row.id,
                "actor_source": row.actor_source,
                "actor_id": row.actor_id,
                "actor_display": row.actor_display,
                "action": row.action,
                "target": row.target,
                "metadata": row.audit_metadata,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def table_counts() -> dict:
        with SessionLocal() as db:
            campaigns = db.query(func.count(Campaign.id)).scalar() or 0
            players = db.query(func.count(Player.id)).scalar() or 0
            turns = db.query(func.count(TurnLog.id)).scalar() or 0
            bots = db.query(func.count(BotConfig.id)).scalar() or 0
            syslogs = db.query(func.count(SystemLog.id)).scalar() or 0
        return {
            "campaigns": int(campaigns),
            "players": int(players),
            "turn_logs": int(turns),
            "bot_configs": int(bots),
            "system_logs": int(syslogs),
        }


def make_handler(state: DBAPIState):
    class DBHandler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n <= 0:
                return {}
            raw = self.rfile.read(n).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def _auth(self) -> bool:
            if state.auth_ok(self.headers.get("Authorization", "")):
                return True
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/db/v1/health":
                    payload = state.health()
                    self._send_json(200 if payload.get("ok") else 503, payload)
                    return
                if path == "/db/v1/bots":
                    enabled = _parse_bool((query.get("enabled", [None]) or [None])[0], default=None)
                    self._send_json(200, {"ok": True, "rows": state.list_bots(enabled)})
                    return
                if path == "/db/v1/logs/system":
                    limit = int((query.get("limit", ["100"]) or ["100"])[0])
                    service = str((query.get("service", [""]) or [""])[0]).strip()
                    level = str((query.get("level", [""]) or [""])[0]).strip().upper()
                    self._send_json(200, {"ok": True, "rows": state.list_system_logs(limit, service, level)})
                    return
                if path == "/db/v1/logs/audit":
                    limit = int((query.get("limit", ["100"]) or ["100"])[0])
                    self._send_json(200, {"ok": True, "rows": state.list_audit_logs(limit)})
                    return
                if path == "/db/v1/debug/table-counts":
                    self._send_json(200, {"ok": True, "counts": state.table_counts()})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_POST(self):  # noqa: N802
            if not self._auth():
                return
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/db/v1/bots":
                    self._send_json(201, {"ok": True, **state.create_bot(payload)})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_PUT(self):  # noqa: N802
            if not self._auth():
                return
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path.startswith("/db/v1/bots/"):
                    bot_id = int(parsed.path.rsplit("/", 1)[-1])
                    self._send_json(200, {"ok": True, **state.update_bot(bot_id, payload)})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_DELETE(self):  # noqa: N802
            if not self._auth():
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith("/db/v1/bots/"):
                    bot_id = int(parsed.path.rsplit("/", 1)[-1])
                    self._send_json(200, {"ok": True, **state.delete_bot(bot_id)})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})

        def log_message(self, _format, *args):
            return

    return DBHandler


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI GameMaster DB API service.")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=settings.db_api_port)
    args = parser.parse_args()
    state = DBAPIState()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(state))
    print(f"[db_api] listening on http://{args.bind}:{args.port}/db/v1/health", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
