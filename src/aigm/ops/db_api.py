from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, text

from aigm.config import settings
from aigm.db.models import (
    AdminAuditLog,
    BotConfig,
    Campaign,
    CampaignRule,
    DiceRollLog,
    EffectKnowledge,
    GlobalEffectRelevance,
    GlobalLearnedRelevance,
    ItemKnowledge,
    Player,
    ProcessedDiscordMessage,
    SystemLog,
    TurnLog,
)
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
    def ingest_system_logs(rows: list[dict]) -> dict:
        cleaned: list[SystemLog] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            service = str(row.get("service", "") or "").strip()
            level = str(row.get("level", "INFO") or "INFO").strip().upper()
            message = str(row.get("message", "") or "")
            source = str(row.get("source", "runtime") or "runtime").strip()
            metadata = row.get("metadata", {})
            if not service or not message:
                continue
            if not isinstance(metadata, dict):
                metadata = {"raw_metadata": str(metadata)}
            cleaned.append(
                SystemLog(
                    service=service,
                    level=level,
                    message=message,
                    source=source,
                    log_metadata=metadata,
                )
            )
        if not cleaned:
            return {"inserted": 0}
        with SessionLocal() as db:
            db.add_all(cleaned)
            db.commit()
        return {"inserted": len(cleaned)}

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

    @staticmethod
    def list_campaigns(limit: int = 200) -> list[dict]:
        with SessionLocal() as db:
            rows = db.query(Campaign).order_by(Campaign.id.desc()).limit(max(1, min(1000, int(limit)))).all()
        return [
            {
                "id": row.id,
                "discord_thread_id": row.discord_thread_id,
                "mode": row.mode,
                "state": row.state,
                "version": row.version,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def list_turn_logs(campaign_id: int, limit: int = 50) -> list[dict]:
        with SessionLocal() as db:
            rows = (
                db.query(TurnLog)
                .filter(TurnLog.campaign_id == int(campaign_id))
                .order_by(TurnLog.id.desc())
                .limit(max(1, min(500, int(limit))))
                .all()
            )
        return [
            {
                "id": row.id,
                "campaign_id": row.campaign_id,
                "actor": row.actor,
                "user_input": row.user_input,
                "ai_raw_output": row.ai_raw_output,
                "accepted_commands": row.accepted_commands,
                "rejected_commands": row.rejected_commands,
                "narration": row.narration,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def list_item_knowledge(limit: int = 200) -> list[dict]:
        with SessionLocal() as db:
            rows = db.query(ItemKnowledge).order_by(ItemKnowledge.observation_count.desc()).limit(max(1, min(1000, int(limit)))).all()
        return [
            {
                "item_key": row.item_key,
                "canonical_name": row.canonical_name,
                "object_type": row.object_type,
                "portability": row.portability,
                "rarity": row.rarity,
                "observation_count": row.observation_count,
                "confidence": float(row.confidence),
                "summary": row.summary,
            }
            for row in rows
        ]

    @staticmethod
    def list_global_item_relevance(limit: int = 300) -> list[dict]:
        with SessionLocal() as db:
            rows = (
                db.query(GlobalLearnedRelevance)
                .order_by(GlobalLearnedRelevance.score.desc())
                .limit(max(1, min(2000, int(limit))))
                .all()
            )
        return [
            {
                "item_key": row.item_key,
                "context_tag": row.context_tag,
                "interaction_count": row.interaction_count,
                "score": float(row.score),
            }
            for row in rows
        ]

    @staticmethod
    def list_effect_knowledge(limit: int = 200) -> list[dict]:
        with SessionLocal() as db:
            rows = db.query(EffectKnowledge).order_by(EffectKnowledge.observation_count.desc()).limit(max(1, min(1000, int(limit)))).all()
        return [
            {
                "effect_key": row.effect_key,
                "canonical_name": row.canonical_name,
                "category": row.category,
                "observation_count": row.observation_count,
                "confidence": float(row.confidence),
                "summary": row.summary,
            }
            for row in rows
        ]

    @staticmethod
    def list_global_effect_relevance(limit: int = 300) -> list[dict]:
        with SessionLocal() as db:
            rows = (
                db.query(GlobalEffectRelevance)
                .order_by(GlobalEffectRelevance.score.desc())
                .limit(max(1, min(2000, int(limit))))
                .all()
            )
        return [
            {
                "effect_key": row.effect_key,
                "context_tag": row.context_tag,
                "interaction_count": row.interaction_count,
                "score": float(row.score),
            }
            for row in rows
        ]

    @staticmethod
    def list_dice_roll_logs(limit: int = 100, campaign_id: int | None = None) -> list[dict]:
        with SessionLocal() as db:
            q = db.query(DiceRollLog)
            if campaign_id is not None and int(campaign_id) > 0:
                q = q.filter(DiceRollLog.campaign_id == int(campaign_id))
            rows = q.order_by(DiceRollLog.id.desc()).limit(max(1, min(1000, int(limit)))).all()
        return [
            {
                "id": row.id,
                "campaign_id": row.campaign_id,
                "actor_display_name": row.actor_display_name,
                "expression": row.expression,
                "normalized_expression": row.normalized_expression,
                "total": row.total,
                "detail_json": row.detail_json,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def campaign_by_thread(thread_id: str) -> dict | None:
        with SessionLocal() as db:
            row = db.query(Campaign).filter(Campaign.discord_thread_id == thread_id).one_or_none()
            if not row:
                return None
            return {
                "id": row.id,
                "discord_thread_id": row.discord_thread_id,
                "mode": row.mode,
                "state": row.state,
                "version": row.version,
            }

    @staticmethod
    def campaign_by_id(campaign_id: int) -> dict | None:
        with SessionLocal() as db:
            row = db.query(Campaign).filter(Campaign.id == int(campaign_id)).one_or_none()
            if not row:
                return None
            return {
                "id": row.id,
                "discord_thread_id": row.discord_thread_id,
                "mode": row.mode,
                "state": row.state,
                "version": row.version,
            }

    @staticmethod
    def upsert_campaign_by_thread(thread_id: str, mode: str, state: dict) -> dict:
        with SessionLocal() as db:
            row = db.query(Campaign).filter(Campaign.discord_thread_id == thread_id).one_or_none()
            if row is None:
                row = Campaign(discord_thread_id=thread_id, mode=mode, state=state)
                db.add(row)
                db.commit()
                db.refresh(row)
            return {
                "id": row.id,
                "discord_thread_id": row.discord_thread_id,
                "mode": row.mode,
                "state": row.state,
                "version": row.version,
            }

    @staticmethod
    def campaign_rules(campaign_id: int) -> dict[str, str]:
        with SessionLocal() as db:
            rows = db.query(CampaignRule).filter(CampaignRule.campaign_id == int(campaign_id)).all()
            return {r.rule_key: r.rule_value for r in rows}

    @staticmethod
    def set_campaign_rule(campaign_id: int, key: str, value: str) -> dict:
        with SessionLocal() as db:
            row = (
                db.query(CampaignRule)
                .filter(CampaignRule.campaign_id == int(campaign_id), CampaignRule.rule_key == key)
                .one_or_none()
            )
            if row:
                row.rule_value = value
                db.add(row)
            else:
                db.add(CampaignRule(campaign_id=int(campaign_id), rule_key=key, rule_value=value))
            db.commit()
        return {"campaign_id": int(campaign_id), "rule_key": key}

    @staticmethod
    def reserve_processed_message(campaign_id: int | None, discord_message_id: str, actor_discord_user_id: str) -> dict:
        with SessionLocal() as db:
            exists = (
                db.query(ProcessedDiscordMessage)
                .filter(ProcessedDiscordMessage.discord_message_id == discord_message_id)
                .one_or_none()
            )
            if exists:
                return {"reserved": False}
            db.add(
                ProcessedDiscordMessage(
                    campaign_id=campaign_id,
                    discord_message_id=discord_message_id,
                    actor_discord_user_id=actor_discord_user_id,
                )
            )
            db.commit()
            return {"reserved": True}


def make_handler(state: DBAPIState):
    class DBHandler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            correlation_id = str(getattr(self, "_correlation_id", "") or "").strip()
            if correlation_id:
                self.send_header("X-Correlation-ID", correlation_id)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _bind_correlation_id(self) -> None:
            existing = str(self.headers.get("X-Correlation-ID", "") or "").strip()
            self._correlation_id = existing or str(uuid4())

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n <= 0:
                return {}
            raw = self.rfile.read(n).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def _error(self, status: int, code: str, message: str, details: dict | None = None) -> None:
            merged_details = dict(details or {})
            merged_details.setdefault("path", str(getattr(self, "path", "") or ""))
            correlation_id = str(getattr(self, "_correlation_id", "") or "").strip()
            if correlation_id:
                merged_details.setdefault("correlation_id", correlation_id)
            self._send_json(
                int(status),
                {
                    "ok": False,
                    "error": str(message),
                    "error_code": str(code),
                    "error_message": str(message),
                    "error_details": merged_details,
                },
            )

        def _auth(self) -> bool:
            if state.auth_ok(self.headers.get("Authorization", "")):
                return True
            self._error(401, "unauthorized", "unauthorized")
            return False

        def do_GET(self):  # noqa: N802
            self._bind_correlation_id()
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
                if path == "/db/v1/campaigns/by-thread":
                    thread_id = str((query.get("thread_id", [""]) or [""])[0]).strip()
                    if not thread_id:
                        self._error(400, "bad_request", "thread_id is required")
                        return
                    row = state.campaign_by_thread(thread_id)
                    self._send_json(200, {"ok": True, "row": row})
                    return
                if path == "/db/v1/campaigns/by-id":
                    campaign_id = int((query.get("campaign_id", ["0"]) or ["0"])[0])
                    if campaign_id <= 0:
                        self._error(400, "bad_request", "campaign_id is required")
                        return
                    row = state.campaign_by_id(campaign_id)
                    self._send_json(200, {"ok": True, "row": row})
                    return
                if path == "/db/v1/campaigns/rules":
                    campaign_id = int((query.get("campaign_id", ["0"]) or ["0"])[0])
                    if campaign_id <= 0:
                        self._error(400, "bad_request", "campaign_id is required")
                        return
                    self._send_json(200, {"ok": True, "rules": state.campaign_rules(campaign_id)})
                    return
                if path == "/db/v1/campaigns":
                    limit = int((query.get("limit", ["200"]) or ["200"])[0])
                    self._send_json(200, {"ok": True, "rows": state.list_campaigns(limit)})
                    return
                if path == "/db/v1/turns":
                    campaign_id = int((query.get("campaign_id", ["0"]) or ["0"])[0])
                    if campaign_id <= 0:
                        self._error(400, "bad_request", "campaign_id is required")
                        return
                    limit = int((query.get("limit", ["50"]) or ["50"])[0])
                    self._send_json(200, {"ok": True, "rows": state.list_turn_logs(campaign_id, limit)})
                    return
                if path == "/db/v1/knowledge/items":
                    limit = int((query.get("limit", ["200"]) or ["200"])[0])
                    self._send_json(200, {"ok": True, "rows": state.list_item_knowledge(limit)})
                    return
                if path == "/db/v1/knowledge/item-relevance":
                    limit = int((query.get("limit", ["300"]) or ["300"])[0])
                    self._send_json(200, {"ok": True, "rows": state.list_global_item_relevance(limit)})
                    return
                if path == "/db/v1/knowledge/effects":
                    limit = int((query.get("limit", ["200"]) or ["200"])[0])
                    self._send_json(200, {"ok": True, "rows": state.list_effect_knowledge(limit)})
                    return
                if path == "/db/v1/knowledge/effect-relevance":
                    limit = int((query.get("limit", ["300"]) or ["300"])[0])
                    self._send_json(200, {"ok": True, "rows": state.list_global_effect_relevance(limit)})
                    return
                if path == "/db/v1/dice-rolls":
                    limit = int((query.get("limit", ["100"]) or ["100"])[0])
                    campaign_id_raw = (query.get("campaign_id", [""]) or [""])[0]
                    campaign_id = int(campaign_id_raw) if str(campaign_id_raw).strip() else None
                    self._send_json(200, {"ok": True, "rows": state.list_dice_roll_logs(limit, campaign_id)})
                    return
                self._error(404, "not_found", "not_found")
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal_error", str(exc))

        def do_POST(self):  # noqa: N802
            self._bind_correlation_id()
            if not self._auth():
                return
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/db/v1/bots":
                    self._send_json(201, {"ok": True, **state.create_bot(payload)})
                    return
                if parsed.path == "/db/v1/logs/system/batch":
                    rows = payload.get("rows", [])
                    if not isinstance(rows, list):
                        raise ValueError("rows must be a list")
                    inserted = state.ingest_system_logs(rows)
                    self._send_json(200, {"ok": True, **inserted})
                    return
                if parsed.path == "/db/v1/campaigns/upsert-thread":
                    thread_id = str(payload.get("thread_id", "")).strip()
                    mode = str(payload.get("mode", "dnd")).strip().lower() or "dnd"
                    state_payload = payload.get("state")
                    if not thread_id:
                        raise ValueError("thread_id is required")
                    if not isinstance(state_payload, dict):
                        raise ValueError("state is required")
                    self._send_json(200, {"ok": True, "row": state.upsert_campaign_by_thread(thread_id, mode, state_payload)})
                    return
                if parsed.path == "/db/v1/campaigns/rules/set":
                    campaign_id = int(payload.get("campaign_id", 0) or 0)
                    key = str(payload.get("rule_key", "")).strip()
                    value = str(payload.get("rule_value", ""))
                    if campaign_id <= 0 or not key:
                        raise ValueError("campaign_id and rule_key are required")
                    self._send_json(200, {"ok": True, **state.set_campaign_rule(campaign_id, key, value)})
                    return
                if parsed.path == "/db/v1/idempotency/reserve":
                    campaign_id_raw = payload.get("campaign_id")
                    campaign_id = int(campaign_id_raw) if campaign_id_raw is not None else None
                    discord_message_id = str(payload.get("discord_message_id", "")).strip()
                    actor_discord_user_id = str(payload.get("actor_discord_user_id", "")).strip()
                    if not discord_message_id or not actor_discord_user_id:
                        raise ValueError("discord_message_id and actor_discord_user_id are required")
                    self._send_json(
                        200,
                        {"ok": True, **state.reserve_processed_message(campaign_id, discord_message_id, actor_discord_user_id)},
                    )
                    return
                self._error(404, "not_found", "not_found")
            except ValueError as exc:
                self._error(400, "bad_request", str(exc))
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal_error", str(exc))

        def do_PUT(self):  # noqa: N802
            self._bind_correlation_id()
            if not self._auth():
                return
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path.startswith("/db/v1/bots/"):
                    bot_id = int(parsed.path.rsplit("/", 1)[-1])
                    self._send_json(200, {"ok": True, **state.update_bot(bot_id, payload)})
                    return
                self._error(404, "not_found", "not_found")
            except ValueError as exc:
                self._error(400, "bad_request", str(exc))
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal_error", str(exc))

        def do_DELETE(self):  # noqa: N802
            self._bind_correlation_id()
            if not self._auth():
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith("/db/v1/bots/"):
                    bot_id = int(parsed.path.rsplit("/", 1)[-1])
                    self._send_json(200, {"ok": True, **state.delete_bot(bot_id)})
                    return
                self._error(404, "not_found", "not_found")
            except ValueError as exc:
                self._error(400, "bad_request", str(exc))
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal_error", str(exc))

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
