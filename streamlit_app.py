from __future__ import annotations

import json
from urllib import error, request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from sqlalchemy import create_engine
from openai import OpenAI

from aigm.adapters.llm import LLMAdapter
from aigm.agents.crew import default_agent_crew_definition
from aigm.config import settings
from aigm.db.models import (
    AdminAuditLog,
    AuthPermission,
    AuthRole,
    AuthRolePermission,
    AuthUser,
    BotConfig,
    Campaign,
    DiceRollLog,
    EffectKnowledge,
    GlobalEffectRelevance,
    GlobalLearnedRelevance,
    ItemKnowledge,
    SystemLog,
    TurnLog,
)
from aigm.db.session import SessionLocal
from aigm.services.game_service import GameService


st.set_page_config(page_title="AI GameMaster Console", layout="wide")
st.title("AI GameMaster Console")
st.caption("Manage campaigns, rules, agents, and admin operations.")

service = GameService(LLMAdapter())


with SessionLocal() as db:
    service.seed_default_auth(db)
    service.seed_default_gameplay_knowledge(db)


def resolve_thread_name_from_discord(thread_id: str) -> str | None:
    token = settings.discord_token.strip()
    if not token:
        return None
    req = request.Request(
        url=f"https://discord.com/api/v10/channels/{thread_id}",
        method="GET",
        headers={"Authorization": f"Bot {token}"},
    )
    try:
        with request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        name = str(data.get("name", "")).strip()
        return name or None
    except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def current_auth_user(db) -> AuthUser | None:
    auth_user_id = st.session_state.get("auth_user_id")
    if not auth_user_id:
        return None
    return db.query(AuthUser).filter(AuthUser.id == int(auth_user_id), AuthUser.is_active.is_(True)).one_or_none()


def ui_has_perm(db, permission: str) -> bool:
    if not settings.auth_enforce:
        return True
    user = current_auth_user(db)
    if not user:
        return False
    return service.auth_user_has_permission(db, user.id, permission)


def require_ui_perm(db, permission: str) -> None:
    if ui_has_perm(db, permission):
        return
    st.error(f"Permission denied: {permission}")
    st.stop()


def audit_ui_action(db, action: str, target: str = "", metadata: dict | None = None) -> None:
    user = current_auth_user(db)
    actor_id = str(user.id) if user else "anonymous"
    actor_display = user.username if user else "anonymous"
    service.audit_admin_action(
        db,
        actor_source="streamlit",
        actor_id=actor_id,
        actor_display=actor_display,
        action=action,
        target=target,
        metadata=metadata or {},
    )


def set_dotenv_value(env_path: str, key: str, value: str) -> None:
    path = Path(env_path)
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


def test_db_url(url: str) -> tuple[bool, str]:
    try:
        engine = create_engine(url, future=True)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True, "Database connection succeeded."
    except Exception as exc:  # noqa: BLE001
        return False, f"Database connection failed: {exc}"


def test_ollama_url(url: str) -> tuple[bool, str]:
    endpoint = f"{url.rstrip('/')}/api/tags"
    req = request.Request(endpoint, method="GET")
    try:
        with request.urlopen(req, timeout=8) as resp:
            _ = json.loads(resp.read().decode("utf-8"))
        return True, "Ollama endpoint reachable."
    except Exception as exc:  # noqa: BLE001
        return False, f"Ollama endpoint failed: {exc}"


def ollama_list_models(url: str) -> tuple[bool, list[str], str]:
    endpoint = f"{url.rstrip('/')}/api/tags"
    req = request.Request(endpoint, method="GET")
    try:
        with request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        models = [str(m.get("name", "")).strip() for m in payload.get("models", []) if str(m.get("name", "")).strip()]
        return True, models, f"Found {len(models)} models."
    except Exception as exc:  # noqa: BLE001
        return False, [], f"Failed to list models: {exc}"


def ollama_pull_model(url: str, model: str) -> tuple[bool, str]:
    endpoint = f"{url.rstrip('/')}/api/pull"
    payload = {"name": model.strip(), "stream": False}
    req = request.Request(
        endpoint,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=3600) as resp:
            _ = resp.read()
        return True, f"Pulled model '{model.strip()}'."
    except Exception as exc:  # noqa: BLE001
        return False, f"Pull failed: {exc}"


def ollama_delete_model(url: str, model: str) -> tuple[bool, str]:
    endpoint = f"{url.rstrip('/')}/api/delete"
    payload = {"name": model.strip()}
    req = request.Request(
        endpoint,
        method="DELETE",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
        return True, f"Deleted model '{model.strip()}'."
    except Exception as exc:  # noqa: BLE001
        return False, f"Delete failed: {exc}"


def openai_list_models(api_key: str, base_url: str) -> tuple[bool, list[str], str]:
    try:
        kwargs: dict = {"api_key": api_key.strip()}
        if base_url.strip():
            kwargs["base_url"] = base_url.strip()
        client = OpenAI(**kwargs)
        rows = client.models.list()
        models = sorted({str(x.id) for x in rows.data})
        return True, models, f"Found {len(models)} models."
    except Exception as exc:  # noqa: BLE001
        return False, [], f"Failed to list models: {exc}"


def fetch_health_payload(url: str) -> tuple[bool, dict | None, str]:
    # Supervisor health endpoint is the source of truth for cross-service readiness.
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return True, payload, "Health endpoint reachable."
    except Exception as exc:  # noqa: BLE001
        return False, None, f"Health endpoint failed: {exc}"


def read_doc_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"Unable to read {path}: {exc}"


with SessionLocal() as db:
    if settings.auth_enforce:
        user = current_auth_user(db)
        if not user:
            st.subheader("Login")
            username = st.text_input("Username", key="login_username")
            password = st.text_input("Password", type="password", key="login_password")
            if st.button("Login", key="login_submit"):
                auth_user = service.auth_authenticate_user(db, username.strip(), password)
                if auth_user:
                    st.session_state["auth_user_id"] = auth_user.id
                    st.success("Authenticated.")
                    st.rerun()
                else:
                    st.error("Invalid credentials.")
            st.stop()

        st.sidebar.success(f"Logged in as {user.username}")
        if st.sidebar.button("Logout", key="sidebar_logout"):
            st.session_state.pop("auth_user_id", None)
            st.rerun()

    page_specs: list[tuple[str, str | None]] = [
        ("Campaign Console", "campaign.read"),
        ("Health", "system.admin"),
        ("LLM Management", "system.admin"),
        ("Gameplay & Knowledge", "campaign.read"),
        ("Item Tracker", "campaign.read"),
        ("System Logs", "system.admin"),
        ("Bot Manager", "system.admin"),
        ("Users & Roles", "user.manage"),
        ("Admin", "system.admin"),
        ("Documentation", None),
    ]
    visible_pages = [name for name, perm in page_specs if perm is None or ui_has_perm(db, perm)]
    if not visible_pages:
        st.error("No pages available for your current permissions.")
        st.stop()
    page = st.sidebar.radio("Page", visible_pages, key="main_page")

    if page == "Health":
        require_ui_perm(db, "system.admin")
        st.subheader("System Health")
        health_url = st.text_input("Health URL", value=settings.healthcheck_url, key="health_url_top")
        if st.button("Run health check", key="health_run_top"):
            ok, payload, detail = fetch_health_payload(health_url.strip())
            if ok and payload is not None:
                st.success(detail)
                st.code(json.dumps(payload, indent=2), language="json")
            else:
                st.error(detail)
                st.caption("Falling back to local direct checks from Streamlit host.")
                db_ok, db_msg = test_db_url(settings.database_url)
                ollama_ok, ollama_msg = test_ollama_url(settings.ollama_url)
                streamlit_ok = True
                fallback_payload = {
                    "ok": db_ok and ollama_ok and streamlit_ok,
                    "checks": {
                        "db": {"ok": db_ok, "detail": db_msg},
                        "ollama": {"ok": ollama_ok, "detail": ollama_msg},
                        "streamlit": {"ok": streamlit_ok, "detail": "this page is running"},
                    },
                }
                st.code(json.dumps(fallback_payload, indent=2), language="json")
        st.stop()

    if page == "LLM Management":
        require_ui_perm(db, "system.admin")
        st.subheader("LLM Management")
        env_file_path = st.text_input("Env file path", value=str(Path.cwd() / ".env"), key="llm_env_path")
        provider = st.selectbox("Provider", ["ollama", "openai", "stub"], index=["ollama", "openai", "stub"].index(settings.llm_provider if settings.llm_provider in {"ollama", "openai", "stub"} else "ollama"), key="llm_provider")
        strict_json = st.checkbox("Strict JSON mode", value=settings.llm_json_mode_strict, key="llm_json_strict")

        st.markdown("### Ollama")
        ollama_url_input = st.text_input("Ollama URL", value=settings.ollama_url, key="llm_ollama_url")
        ollama_model_input = st.text_input("Default model", value=settings.ollama_model, key="llm_ollama_model")
        col_ol1, col_ol2, col_ol3 = st.columns(3)
        with col_ol1:
            if st.button("Test Ollama", key="llm_test_ollama"):
                ok, msg = test_ollama_url(ollama_url_input.strip())
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)
        with col_ol2:
            if st.button("List Ollama models", key="llm_list_ollama"):
                ok, models, msg = ollama_list_models(ollama_url_input.strip())
                if ok:
                    st.success(msg)
                    st.code("\n".join(models) if models else "(no models)")
                else:
                    st.error(msg)
        with col_ol3:
            pull_model = st.text_input("Model to pull", value=ollama_model_input, key="llm_pull_model")
            if st.button("Pull model", key="llm_pull_btn"):
                ok, msg = ollama_pull_model(ollama_url_input.strip(), pull_model.strip())
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)
        delete_model = st.text_input("Model to delete", key="llm_delete_model")
        if st.button("Delete model", key="llm_delete_btn"):
            ok, msg = ollama_delete_model(ollama_url_input.strip(), delete_model.strip())
            if ok:
                st.success(msg)
            else:
                st.error(msg)

        st.markdown("### OpenAI / Compatible API")
        openai_api_key = st.text_input("OpenAI API key", value=settings.openai_api_key, type="password", key="llm_openai_key")
        openai_base_url = st.text_input("OpenAI base URL (optional)", value=settings.openai_base_url, key="llm_openai_base")
        openai_model = st.text_input("Default OpenAI model", value=settings.openai_model, key="llm_openai_model")
        if st.button("List OpenAI models", key="llm_list_openai"):
            ok, models, msg = openai_list_models(openai_api_key, openai_base_url)
            if ok:
                st.success(msg)
                st.code("\n".join(models) if models else "(no models)")
            else:
                st.error(msg)

        st.markdown("### Task Models")
        c_tm1, c_tm2, c_tm3 = st.columns(3)
        with c_tm1:
            o_narr = st.text_input("Ollama narration model", value=settings.ollama_model_narration, key="llm_ollama_narr")
            oa_narr = st.text_input("OpenAI narration model", value=settings.openai_model_narration, key="llm_openai_narr")
        with c_tm2:
            o_int = st.text_input("Ollama intent model", value=settings.ollama_model_intent, key="llm_ollama_int")
            oa_int = st.text_input("OpenAI intent model", value=settings.openai_model_intent, key="llm_openai_int")
        with c_tm3:
            o_rev = st.text_input("Ollama review model", value=settings.ollama_model_review, key="llm_ollama_rev")
            oa_rev = st.text_input("OpenAI review model", value=settings.openai_model_review, key="llm_openai_rev")

        st.markdown("### Generation / JSON Settings")
        c_set1, c_set2 = st.columns(2)
        with c_set1:
            ollama_timeout = st.number_input("Ollama timeout (s)", min_value=5, max_value=3600, value=int(settings.ollama_timeout_s), key="llm_ollama_timeout")
            openai_timeout = st.number_input("OpenAI timeout (s)", min_value=5, max_value=3600, value=int(settings.openai_timeout_s), key="llm_openai_timeout")
            gen_temp = st.number_input("Gen temperature", min_value=0.0, max_value=2.0, value=float(settings.ollama_gen_temperature), step=0.05, key="llm_gen_temp")
            json_temp = st.number_input("JSON temperature", min_value=0.0, max_value=2.0, value=float(settings.ollama_json_temperature), step=0.05, key="llm_json_temp")
        with c_set2:
            gen_tokens = st.number_input("Gen token limit (ollama num_predict)", min_value=16, max_value=8192, value=int(settings.ollama_gen_num_predict), step=16, key="llm_gen_tokens")
            json_tokens = st.number_input("JSON token limit (ollama num_predict)", min_value=16, max_value=8192, value=int(settings.ollama_json_num_predict), step=16, key="llm_json_tokens")

        if st.button("Save LLM settings", key="llm_save"):
            set_dotenv_value(env_file_path, "AIGM_LLM_PROVIDER", provider.strip())
            set_dotenv_value(env_file_path, "AIGM_LLM_JSON_MODE_STRICT", "true" if strict_json else "false")
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_URL", ollama_url_input.strip())
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_MODEL", ollama_model_input.strip())
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_MODEL_NARRATION", o_narr.strip())
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_MODEL_INTENT", o_int.strip())
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_MODEL_REVIEW", o_rev.strip())
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_TIMEOUT_S", str(int(ollama_timeout)))
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_GEN_TEMPERATURE", str(float(gen_temp)))
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_JSON_TEMPERATURE", str(float(json_temp)))
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_GEN_NUM_PREDICT", str(int(gen_tokens)))
            set_dotenv_value(env_file_path, "AIGM_OLLAMA_JSON_NUM_PREDICT", str(int(json_tokens)))
            set_dotenv_value(env_file_path, "AIGM_OPENAI_API_KEY", openai_api_key.strip())
            set_dotenv_value(env_file_path, "AIGM_OPENAI_BASE_URL", openai_base_url.strip())
            set_dotenv_value(env_file_path, "AIGM_OPENAI_MODEL", openai_model.strip())
            set_dotenv_value(env_file_path, "AIGM_OPENAI_MODEL_NARRATION", oa_narr.strip())
            set_dotenv_value(env_file_path, "AIGM_OPENAI_MODEL_INTENT", oa_int.strip())
            set_dotenv_value(env_file_path, "AIGM_OPENAI_MODEL_REVIEW", oa_rev.strip())
            set_dotenv_value(env_file_path, "AIGM_OPENAI_TIMEOUT_S", str(int(openai_timeout)))
            audit_ui_action(
                db,
                action="llm_settings_saved",
                target=provider.strip(),
                metadata={
                    "env_file_path": env_file_path,
                    "strict_json": bool(strict_json),
                    "ollama_url": ollama_url_input.strip(),
                    "openai_base_url_set": bool(openai_base_url.strip()),
                },
            )
            st.success("Saved LLM settings. Restart services to apply changes.")
        st.stop()

    if page == "Item Tracker":
        require_ui_perm(db, "campaign.read")
        st.subheader("Global Item Knowledge")
        items = db.query(ItemKnowledge).order_by(ItemKnowledge.observation_count.desc()).limit(200).all()
        st.dataframe(
            [
                {
                    "item_key": i.item_key,
                    "canonical_name": i.canonical_name,
                    "type": i.object_type,
                    "portability": i.portability,
                    "rarity": i.rarity,
                    "observations": i.observation_count,
                    "confidence": round(float(i.confidence), 3),
                    "summary": i.summary,
                }
                for i in items
            ],
            width="stretch",
        )
        st.subheader("Global Learned Relevance")
        rel = db.query(GlobalLearnedRelevance).order_by(GlobalLearnedRelevance.score.desc()).limit(300).all()
        st.dataframe(
            [{"item_key": r.item_key, "context_tag": r.context_tag, "interactions": r.interaction_count, "score": round(float(r.score), 3)} for r in rel],
            width="stretch",
        )
        st.subheader("Global Effect Knowledge")
        effects = db.query(EffectKnowledge).order_by(EffectKnowledge.observation_count.desc()).limit(200).all()
        st.dataframe(
            [
                {
                    "effect_key": e.effect_key,
                    "canonical_name": e.canonical_name,
                    "category": e.category,
                    "observations": e.observation_count,
                    "confidence": round(float(e.confidence), 3),
                    "summary": e.summary,
                }
                for e in effects
            ],
            width="stretch",
        )
        st.subheader("Global Effect Relevance")
        erel = db.query(GlobalEffectRelevance).order_by(GlobalEffectRelevance.score.desc()).limit(300).all()
        st.dataframe(
            [{"effect_key": r.effect_key, "context_tag": r.context_tag, "interactions": r.interaction_count, "score": round(float(r.score), 3)} for r in erel],
            width="stretch",
        )
        st.stop()

    if page == "Gameplay & Knowledge":
        require_ui_perm(db, "campaign.read")
        st.subheader("Gameplay & Knowledge")

        campaigns = db.query(Campaign).order_by(Campaign.id.desc()).all()
        campaign_map = {f"{c.id} | {c.discord_thread_id} | {c.mode}": c for c in campaigns}
        selected_campaign = None
        if campaign_map:
            selected_campaign = campaign_map[st.selectbox("Campaign", list(campaign_map.keys()), key="gk_campaign_pick")]

        tab_assign, tab_rulesets, tab_rulebooks, tab_dice = st.tabs(
            ["Campaign Ruleset", "Rulesets", "Rulebooks", "Dice Logs"]
        )

        with tab_assign:
            if not selected_campaign:
                st.info("No campaigns found.")
            else:
                active = service._ruleset_for_campaign(db, selected_campaign)
                st.write(f"Current ruleset: `{active.key if active else 'none'}`")
                options = service.list_game_rulesets(db, enabled_only=True)
                option_keys = [r.key for r in options]
                if option_keys:
                    pick = st.selectbox("Assign ruleset", option_keys, key="gk_assign_ruleset")
                    if st.button("Save campaign ruleset", key="gk_assign_save"):
                        require_ui_perm(db, "campaign.write")
                        ok, detail = service.set_campaign_ruleset(db, selected_campaign, pick)
                        if ok:
                            audit_ui_action(
                                db,
                                action="campaign_ruleset_set",
                                target=f"{selected_campaign.id}:{detail}",
                            )
                            st.success(f"Campaign ruleset set to `{detail}`.")
                            st.rerun()
                        else:
                            st.error(detail)

        with tab_rulesets:
            rows = service.list_game_rulesets(db, enabled_only=False)
            st.dataframe(
                [
                    {
                        "key": r.key,
                        "name": r.name,
                        "system": r.system,
                        "version": r.version,
                        "official": r.is_official,
                        "enabled": r.is_enabled,
                        "summary": r.summary,
                    }
                    for r in rows
                ],
                width="stretch",
            )
            st.markdown("### Upsert ruleset")
            rk = st.text_input("Ruleset key", key="gk_ruleset_key")
            rn = st.text_input("Name", key="gk_ruleset_name")
            rsys = st.text_input("System", value="dnd", key="gk_ruleset_system")
            rver = st.text_input("Version", key="gk_ruleset_version")
            rsum = st.text_area("Summary", key="gk_ruleset_summary", height=80)
            roff = st.checkbox("Official", value=False, key="gk_ruleset_official")
            ren = st.checkbox("Enabled", value=True, key="gk_ruleset_enabled")
            rjson = st.text_area("Rules JSON", value="{}", key="gk_ruleset_json", height=120)
            if st.button("Upsert ruleset", key="gk_ruleset_save"):
                require_ui_perm(db, "campaign.write")
                try:
                    parsed_rules = json.loads(rjson or "{}")
                    if not isinstance(parsed_rules, dict):
                        raise ValueError("Rules JSON must be an object.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Invalid rules JSON: {exc}")
                else:
                    ok, detail = service.upsert_game_ruleset(
                        db,
                        key=rk,
                        name=rn,
                        system=rsys,
                        version=rver,
                        summary=rsum,
                        is_official=roff,
                        is_enabled=ren,
                        rules_json=parsed_rules,
                    )
                    if ok:
                        audit_ui_action(db, action="ruleset_upserted", target=detail)
                        st.success(f"Ruleset upserted: `{detail}`")
                        st.rerun()
                    else:
                        st.error(detail)

        with tab_rulebooks:
            books = service.list_rulebooks(db, enabled_only=False)
            st.dataframe(
                [
                    {
                        "slug": b.slug,
                        "title": b.title,
                        "system": b.system,
                        "version": b.version,
                        "source": b.source,
                        "enabled": b.is_enabled,
                        "summary": b.summary,
                    }
                    for b in books
                ],
                width="stretch",
            )
            search_q = st.text_input("Lookup query", key="gk_rulelookup_query")
            if st.button("Search rulebook entries", key="gk_rulelookup_btn"):
                rows = service.search_rulebook_entries(db, search_q, limit=6)
                if not rows:
                    st.info("No matching entries.")
                else:
                    st.code(json.dumps(rows, indent=2), language="json")

            st.markdown("### Upsert rulebook")
            b_slug = st.text_input("Rulebook slug", key="gk_book_slug")
            b_title = st.text_input("Title", key="gk_book_title")
            b_system = st.text_input("System", value="dnd", key="gk_book_system")
            b_version = st.text_input("Version", key="gk_book_version")
            b_source = st.text_input("Source", key="gk_book_source")
            b_summary = st.text_area("Summary", key="gk_book_summary", height=80)
            b_enabled = st.checkbox("Enabled", value=True, key="gk_book_enabled")
            if st.button("Upsert rulebook", key="gk_book_save"):
                require_ui_perm(db, "campaign.write")
                ok, detail = service.upsert_rulebook(
                    db,
                    slug=b_slug,
                    title=b_title,
                    system=b_system,
                    version=b_version,
                    source=b_source,
                    summary=b_summary,
                    is_enabled=b_enabled,
                )
                if ok:
                    audit_ui_action(db, action="rulebook_upserted", target=detail)
                    st.success(f"Rulebook upserted: `{detail}`")
                    st.rerun()
                else:
                    st.error(detail)

            st.markdown("### Upsert rulebook entry")
            e_book = st.text_input("Rulebook slug (entry)", key="gk_entry_book")
            e_key = st.text_input("Entry key", key="gk_entry_key")
            e_title = st.text_input("Entry title", key="gk_entry_title")
            e_section = st.text_input("Section", key="gk_entry_section")
            e_page = st.text_input("Page ref", key="gk_entry_page")
            e_tags = st.text_input("Tags (csv)", key="gk_entry_tags")
            e_content = st.text_area("Content", key="gk_entry_content", height=120)
            if st.button("Upsert entry", key="gk_entry_save"):
                require_ui_perm(db, "campaign.write")
                tags = [x.strip() for x in e_tags.split(",") if x.strip()]
                ok, detail = service.upsert_rulebook_entry(
                    db,
                    rulebook_slug=e_book,
                    entry_key=e_key,
                    title=e_title,
                    section=e_section,
                    page_ref=e_page,
                    tags=tags,
                    content=e_content,
                )
                if ok:
                    audit_ui_action(db, action="rulebook_entry_upserted", target=f"{e_book}:{detail}")
                    st.success(f"Entry upserted: `{detail}`")
                    st.rerun()
                else:
                    st.error(detail)

        with tab_dice:
            expr = st.text_input("Dice expression", value="d20", key="gk_roll_expr")
            if st.button("Test roll", key="gk_roll_btn"):
                ok, payload = service.roll_dice(expr)
                if ok:
                    st.code(json.dumps(payload, indent=2), language="json")
                else:
                    st.error(str(payload.get("error", "Invalid roll")))
            logs = db.query(DiceRollLog).order_by(DiceRollLog.id.desc()).limit(100).all()
            st.dataframe(
                [
                    {
                        "id": r.id,
                        "campaign_id": r.campaign_id,
                        "actor": r.actor_display_name,
                        "expression": r.expression,
                        "normalized": r.normalized_expression,
                        "total": r.total,
                        "created_at": str(r.created_at),
                    }
                    for r in logs
                ],
                width="stretch",
            )
        st.stop()

    if page == "System Logs":
        require_ui_perm(db, "system.admin")
        st.subheader("System Logs")
        service_options = ["all"] + [s[0] for s in db.query(SystemLog.service).distinct().order_by(SystemLog.service.asc()).all()]
        level_options = ["all", "DEBUG", "INFO", "WARNING", "ERROR"]
        c_log1, c_log2, c_log3, c_log4 = st.columns(4)
        with c_log1:
            service_filter = st.selectbox("Service", service_options, key="syslog_service_top")
        with c_log2:
            level_filter = st.selectbox("Level", level_options, index=1, key="syslog_level_top")
        with c_log3:
            hours_back = st.number_input("Hours back", min_value=1, max_value=168, value=24, step=1, key="syslog_hours_top")
        with c_log4:
            row_limit = st.number_input("Row limit", min_value=10, max_value=2000, value=250, step=10, key="syslog_limit_top")
        search_text = st.text_input("Message contains", key="syslog_search_top")

        query = db.query(SystemLog)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(hours_back))
        query = query.filter(SystemLog.created_at >= cutoff)
        if service_filter != "all":
            query = query.filter(SystemLog.service == service_filter)
        if level_filter != "all":
            query = query.filter(SystemLog.level == level_filter)
        if search_text.strip():
            query = query.filter(SystemLog.message.ilike(f"%{search_text.strip()}%"))
        rows = query.order_by(SystemLog.id.desc()).limit(int(row_limit)).all()
        st.dataframe(
            [{"id": r.id, "created_at": str(r.created_at), "service": r.service, "level": r.level, "source": r.source, "message": r.message} for r in rows],
            width="stretch",
        )
        st.subheader("Admin Audit Logs")
        audit_limit = st.number_input("Audit row limit", min_value=10, max_value=1000, value=200, step=10, key="audit_limit_top")
        audit_rows = db.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(int(audit_limit)).all()
        st.dataframe(
            [
                {
                    "id": r.id,
                    "created_at": str(r.created_at),
                    "actor_source": r.actor_source,
                    "actor_id": r.actor_id,
                    "actor_display": r.actor_display,
                    "action": r.action,
                    "target": r.target,
                    "metadata": r.audit_metadata,
                }
                for r in audit_rows
            ],
            width="stretch",
        )
        st.stop()

    if page == "Bot Manager":
        require_ui_perm(db, "system.admin")
        st.subheader("Discord Bot Manager")
        bot_rows = db.query(BotConfig).order_by(BotConfig.id.asc()).all()
        st.dataframe(
            [{"id": b.id, "name": b.name, "enabled": b.is_enabled, "notes": b.notes, "created_at": str(b.created_at), "updated_at": str(b.updated_at)} for b in bot_rows],
            width="stretch",
        )
        selected_bot_id = st.selectbox("Select bot config", [0] + [b.id for b in bot_rows], key="bot_cfg_pick_top")
        selected_bot = next((b for b in bot_rows if b.id == int(selected_bot_id)), None)
        bot_name = st.text_input("Bot name", value=selected_bot.name if selected_bot else "", key="bot_cfg_name_top")
        bot_token = st.text_input("Discord bot token", value=selected_bot.discord_token if selected_bot else "", type="password", key="bot_cfg_token_top")
        bot_enabled = st.checkbox("Enabled", value=selected_bot.is_enabled if selected_bot else True, key="bot_cfg_enabled_top")
        bot_notes = st.text_area("Notes", value=selected_bot.notes if selected_bot else "", height=80, key="bot_cfg_notes_top")
        c_bot1, c_bot2, c_bot3 = st.columns(3)
        with c_bot1:
            if st.button("Save bot config", key="bot_cfg_save_top"):
                if selected_bot:
                    selected_bot.name = bot_name.strip()
                    selected_bot.discord_token = bot_token.strip()
                    selected_bot.is_enabled = bool(bot_enabled)
                    selected_bot.notes = bot_notes.strip()
                else:
                    db.add(BotConfig(name=bot_name.strip(), discord_token=bot_token.strip(), is_enabled=bool(bot_enabled), notes=bot_notes.strip()))
                db.commit()
                audit_ui_action(
                    db,
                    action="bot_config_saved",
                    target=bot_name.strip() or (selected_bot.name if selected_bot else ""),
                    metadata={"selected_bot_id": int(selected_bot_id), "enabled": bool(bot_enabled)},
                )
                st.rerun()
        with c_bot2:
            if st.button("Toggle selected bot", key="bot_cfg_toggle_top") and selected_bot:
                selected_bot.is_enabled = not selected_bot.is_enabled
                db.commit()
                audit_ui_action(
                    db,
                    action="bot_config_toggled",
                    target=selected_bot.name,
                    metadata={"bot_id": selected_bot.id, "enabled": selected_bot.is_enabled},
                )
                st.rerun()
        with c_bot3:
            if st.button("Delete selected bot", key="bot_cfg_delete_top") and selected_bot:
                deleted_name = selected_bot.name
                deleted_id = selected_bot.id
                db.delete(selected_bot)
                db.commit()
                audit_ui_action(
                    db,
                    action="bot_config_deleted",
                    target=deleted_name,
                    metadata={"bot_id": deleted_id},
                )
                st.rerun()
        st.stop()

    if page == "Documentation":
        st.subheader("Documentation")
        docs_dir = Path("docs")
        doc_files = sorted(docs_dir.glob("*.md"), key=lambda p: p.name.lower())
        if not doc_files:
            st.warning("No documentation files found in ./docs")
            st.stop()
        docs_map = {p.stem: str(p).replace("\\", "/") for p in doc_files}
        doc_section = st.selectbox("Section", list(docs_map.keys()), key="docs_section_top")
        st.markdown(read_doc_file(docs_map[doc_section]))
        st.stop()

    if page == "Admin":
        require_ui_perm(db, "system.admin")
        st.subheader("Admin")
        st.markdown("### Connection Management")
        env_file_path = st.text_input("Env file path", value=str(Path.cwd() / ".env"), key="conn_env_path_top")
        db_url_input = st.text_input("Database URL", value=settings.database_url, key="conn_db_url_top")
        db_sslmode_input = st.text_input("DB SSL mode", value=settings.database_sslmode, key="conn_db_sslmode_top")
        c_conn1, c_conn2 = st.columns(2)
        with c_conn1:
            if st.button("Test DB connection", key="conn_test_db_top"):
                ok, msg = test_db_url(db_url_input.strip())
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)
        with c_conn2:
            if st.button("Save connection settings", key="conn_save_top"):
                set_dotenv_value(env_file_path, "AIGM_DATABASE_URL", db_url_input.strip())
                set_dotenv_value(env_file_path, "AIGM_DATABASE_SSLMODE", db_sslmode_input.strip())
                audit_ui_action(
                    db,
                    action="db_connection_settings_saved",
                    target=db_sslmode_input.strip(),
                    metadata={"env_file_path": env_file_path},
                )
                st.success("Saved DB settings. Use LLM Management page for provider/model changes.")
        st.markdown("### Auth Users")
        st.dataframe(service.auth_list_users(db), width="stretch")
        st.stop()

    if page == "Users & Roles":
        require_ui_perm(db, "user.manage")
        st.subheader("Users & Roles")

        st.markdown("### Users")
        st.dataframe(service.auth_list_users(db), width="stretch")

        role_rows = db.query(AuthRole).order_by(AuthRole.name.asc()).all()
        role_names = [r.name for r in role_rows]
        perm_rows = db.query(AuthPermission).order_by(AuthPermission.name.asc()).all()
        perm_names = [p.name for p in perm_rows]
        role_by_name = {r.name: r for r in role_rows}

        st.markdown("### Create User")
        c_user1, c_user2, c_user3, c_user4 = st.columns(4)
        with c_user1:
            new_user = st.text_input("Username", key="users_new_username")
        with c_user2:
            new_pwd = st.text_input("Password", type="password", key="users_new_password")
        with c_user3:
            new_display = st.text_input("Display name", key="users_new_display")
        with c_user4:
            new_roles = st.multiselect("Roles", role_names, default=["player"] if "player" in role_names else [], key="users_new_roles")
        if st.button("Create user", key="users_create_btn"):
            ok, detail = service.auth_create_user(
                db,
                username=new_user.strip(),
                password=new_pwd,
                display_name=new_display.strip(),
                roles=new_roles or ["player"],
            )
            if ok:
                audit_ui_action(
                    db,
                    action="auth_user_created",
                    target=new_user.strip().lower(),
                    metadata={"roles": new_roles or ["player"]},
                )
                st.success(detail)
                st.rerun()
            else:
                st.error(detail)

        st.markdown("### Manage User")
        m_user = st.text_input("Existing username", key="users_manage_username")
        c_m1, c_m2, c_m3 = st.columns(3)
        with c_m1:
            assign_role = st.selectbox("Assign role", role_names, key="users_assign_role")
            if st.button("Assign role", key="users_assign_role_btn"):
                ok, detail = service.auth_assign_role(db, m_user.strip(), assign_role)
                if ok:
                    audit_ui_action(
                        db,
                        action="auth_role_assigned",
                        target=m_user.strip().lower(),
                        metadata={"role": assign_role},
                    )
                    st.success(detail)
                else:
                    st.error(detail)
                if ok:
                    st.rerun()
        with c_m2:
            reset_pwd = st.text_input("New password", type="password", key="users_reset_password")
            if st.button("Reset password", key="users_reset_password_btn"):
                ok, detail = service.auth_set_user_password(db, m_user.strip(), reset_pwd)
                if ok:
                    audit_ui_action(
                        db,
                        action="auth_password_reset",
                        target=m_user.strip().lower(),
                    )
                    st.success(detail)
                else:
                    st.error(detail)
        with c_m3:
            discord_id = st.text_input("Discord user id", key="users_link_discord_id")
            if st.button("Link Discord user", key="users_link_discord_btn"):
                ok, detail = service.auth_link_discord_user(db, m_user.strip(), discord_id.strip())
                if ok:
                    audit_ui_action(
                        db,
                        action="auth_discord_linked",
                        target=m_user.strip().lower(),
                        metadata={"discord_user_id": discord_id.strip()},
                    )
                    st.success(detail)
                else:
                    st.error(detail)

        st.markdown("### Roles")
        st.dataframe(
            [{"name": r.name, "description": r.description, "id": r.id} for r in role_rows],
            width="stretch",
        )

        st.markdown("### Create Role")
        c_r1, c_r2 = st.columns(2)
        with c_r1:
            new_role_name = st.text_input("Role name", key="roles_new_name")
            new_role_desc = st.text_input("Role description", key="roles_new_desc")
        with c_r2:
            new_role_perms = st.multiselect("Role permissions", perm_names, key="roles_new_perms")
        if st.button("Create role", key="roles_create_btn"):
            role_name = new_role_name.strip().lower()
            if not role_name:
                st.error("Role name is required.")
            elif db.query(AuthRole).filter(AuthRole.name == role_name).one_or_none():
                st.error("Role already exists.")
            else:
                role = AuthRole(name=role_name, description=new_role_desc.strip())
                db.add(role)
                db.flush()
                for pname in new_role_perms:
                    perm = db.query(AuthPermission).filter(AuthPermission.name == pname).one_or_none()
                    if perm:
                        db.add(AuthRolePermission(role_id=role.id, permission_id=perm.id))
                db.commit()
                audit_ui_action(
                    db,
                    action="auth_role_created",
                    target=role_name,
                    metadata={"permissions": new_role_perms},
                )
                st.success("Role created.")
                st.rerun()

        st.markdown("### Edit Role Permissions")
        selected_role_name = st.selectbox("Role", role_names, key="roles_edit_pick")
        selected_role = role_by_name.get(selected_role_name)
        if selected_role:
            current_perm_ids = {
                rp.permission_id for rp in db.query(AuthRolePermission).filter(AuthRolePermission.role_id == selected_role.id).all()
            }
            current_perm_names = [p.name for p in perm_rows if p.id in current_perm_ids]
            desired_perms = st.multiselect("Permissions", perm_names, default=current_perm_names, key="roles_edit_perms")
            if st.button("Save role permissions", key="roles_edit_save"):
                desired_perm_ids = {
                    p.id for p in perm_rows if p.name in set(desired_perms)
                }
                existing_links = db.query(AuthRolePermission).filter(AuthRolePermission.role_id == selected_role.id).all()
                existing_perm_ids = {x.permission_id for x in existing_links}
                for perm_id in desired_perm_ids - existing_perm_ids:
                    db.add(AuthRolePermission(role_id=selected_role.id, permission_id=perm_id))
                for link in existing_links:
                    if link.permission_id not in desired_perm_ids:
                        db.delete(link)
                db.commit()
                audit_ui_action(
                    db,
                    action="auth_role_permissions_updated",
                    target=selected_role_name,
                    metadata={"permissions": desired_perms},
                )
                st.success("Role permissions updated.")
                st.rerun()
        st.stop()

    campaigns = db.query(Campaign).order_by(Campaign.id.desc()).all()
    if not campaigns:
        st.warning("No campaigns found yet. Start a Discord thread first so a campaign row exists.")
        st.stop()

    campaign_lookup: dict[str, int] = {}
    for c in campaigns:
        campaign_rules = service.list_rules(db, c)
        thread_name = campaign_rules.get("thread_name", "").strip()
        if not thread_name:
            resolved_name = resolve_thread_name_from_discord(c.discord_thread_id)
            if resolved_name:
                thread_name = resolved_name
                service.set_rule(db, c, "thread_name", resolved_name)
        label = f"{thread_name if thread_name else '(unknown thread name)'} | {c.discord_thread_id} (id={c.id}, mode={c.mode})"
        campaign_lookup[label] = c.id

selected_label = st.selectbox("Campaign", list(campaign_lookup.keys()))
campaign_id = campaign_lookup[selected_label]

with SessionLocal() as db:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).one()
    rules = service.list_rules(db, campaign)

    tabs = st.tabs(
        [
            "State",
            "Campaign Rules",
            "Agency Rules",
            "Agent Builder",
            "Crew Run",
            "Turn Logs",
        ]
    )
    tab_state, tab_rules, tab_agency, tab_agents, tab_run, tab_logs = tabs

    with tab_state:
        require_ui_perm(db, "campaign.read")
        st.subheader("Current World State")
        st.code(json.dumps(campaign.state, indent=2), language="json")

    with tab_rules:
        require_ui_perm(db, "campaign.read")
        st.subheader("Campaign Rule Overrides")
        st.dataframe([{"key": k, "value": v} for k, v in sorted(rules.items())], width="stretch")
        st.markdown("### Upsert rule")
        rule_key = st.text_input("Rule key", key="rule_key")
        rule_value = st.text_area("Rule value", key="rule_value", height=120)
        if st.button("Save campaign rule", key="save_rule"):
            require_ui_perm(db, "campaign.write")
            if not rule_key.strip():
                st.error("Rule key is required.")
            else:
                service.set_rule(db, campaign, rule_key.strip(), rule_value)
                audit_ui_action(
                    db,
                    action="campaign_rule_saved",
                    target=f"{campaign.id}:{rule_key.strip()}",
                )
                st.success(f"Saved rule: {rule_key.strip()}")
                st.rerun()

    with tab_agency:
        require_ui_perm(db, "campaign.read")
        st.subheader("Agency Rule Blocks")
        blocks = service.admin_list_rule_blocks(db)
        blocks_by_id = {b.rule_id: b for b in blocks}
        st.dataframe(
            [{"rule_id": b.rule_id, "title": b.title, "priority": b.priority, "enabled": b.is_enabled, "body": b.body} for b in blocks],
            width="stretch",
        )
        selected_rule_id = st.selectbox("Select rule to edit", [""] + [b.rule_id for b in blocks], key="agency_pick")
        selected_block = blocks_by_id.get(selected_rule_id)
        agency_rule_id = st.text_input("Rule ID", value=selected_block.rule_id if selected_block else "", key="agency_rule_id")
        agency_title = st.text_input("Title", value=selected_block.title if selected_block else "", key="agency_title")
        agency_priority = st.selectbox(
            "Priority",
            ["critical", "high", "medium", "low"],
            index=["critical", "high", "medium", "low"].index(selected_block.priority) if selected_block else 1,
            key="agency_priority",
        )
        agency_body = st.text_area("Body", value=selected_block.body if selected_block else "", height=220, key="agency_body")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Upsert agency rule", key="agency_upsert"):
                require_ui_perm(db, "rules.manage")
                service.admin_upsert_rule_block(
                    db,
                    rule_id=agency_rule_id.strip(),
                    title=agency_title.strip(),
                    priority=agency_priority,
                    body=agency_body.strip(),
                )
                audit_ui_action(
                    db,
                    action="agency_rule_upserted",
                    target=agency_rule_id.strip(),
                    metadata={"priority": agency_priority},
                )
                st.success("Upserted.")
                st.rerun()
        with c2:
            if st.button("Remove selected rule", key="agency_remove"):
                require_ui_perm(db, "rules.manage")
                if not selected_block:
                    st.error("Pick an existing rule to remove.")
                else:
                    service.admin_remove_rule_block(db, selected_block.rule_id)
                    audit_ui_action(
                        db,
                        action="agency_rule_removed",
                        target=selected_block.rule_id,
                    )
                    st.success("Removed.")
                    st.rerun()
        with c3:
            if st.button("Toggle selected rule", key="agency_toggle"):
                require_ui_perm(db, "rules.manage")
                if not selected_block:
                    st.error("Pick an existing rule.")
                else:
                    service.admin_set_rule_block_enabled(db, selected_block.rule_id, is_enabled=not selected_block.is_enabled)
                    audit_ui_action(
                        db,
                        action="agency_rule_toggled",
                        target=selected_block.rule_id,
                        metadata={"enabled": not selected_block.is_enabled},
                    )
                    st.success("Updated.")
                    st.rerun()

    with tab_agents:
        require_ui_perm(db, "campaign.read")
        st.subheader("Crew Definition")
        current_engine = service.turn_engine_for_campaign(db, campaign)
        engine_choice = st.radio(
            "Turn engine",
            ["classic", "crew"],
            index=0 if current_engine == "classic" else 1,
            horizontal=True,
            key="turn_engine_choice",
        )
        if st.button("Save turn engine", key="save_turn_engine"):
            require_ui_perm(db, "campaign.write")
            service.set_rule(db, campaign, "turn_engine", engine_choice)
            st.success("Saved.")
            st.rerun()
        default_json = default_agent_crew_definition().model_dump_json(indent=2)
        stored_json = rules.get("agent_crew_definition", default_json)
        crew_json = st.text_area("Crew definition JSON", value=stored_json, height=320, key="crew_json")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Validate crew JSON", key="crew_validate"):
                try:
                    service.crew.parse_definition(crew_json)
                    st.success("Crew definition is valid.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Invalid JSON/shape: {exc}")
        with c2:
            if st.button("Save crew definition", key="crew_save"):
                require_ui_perm(db, "campaign.write")
                try:
                    service.crew.parse_definition(crew_json)
                    service.set_rule(db, campaign, "agent_crew_definition", crew_json)
                    st.success("Saved.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Invalid JSON/shape: {exc}")

    with tab_run:
        require_ui_perm(db, "campaign.play")
        st.subheader("Crew Turn Runner")
        actor_id = st.text_input("Actor ID", value="ui-user")
        actor_name = st.text_input("Actor display name", value="UI User")
        user_input = st.text_area("User input", height=100)
        active_crew_json = rules.get("agent_crew_definition", default_agent_crew_definition().model_dump_json())
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Dry run crew turn", key="crew_dry_run"):
                preview = service.preview_turn_with_crew(
                    db,
                    campaign=campaign,
                    actor=actor_id.strip(),
                    user_input=user_input.strip(),
                    crew_definition_json=active_crew_json,
                )
                st.write(preview["narration"])
                st.code(json.dumps(preview["accepted"], indent=2), language="json")
                st.code(json.dumps(preview["rejected"], indent=2), language="json")
                st.code(json.dumps(preview["crew_outputs"], indent=2), language="json")
        with c2:
            if st.button("Apply crew turn", key="crew_apply"):
                narration, details = service.process_turn_with_crew(
                    db,
                    campaign=campaign,
                    actor=actor_id.strip(),
                    actor_display_name=actor_name.strip() or actor_id.strip(),
                    user_input=user_input.strip(),
                    crew_definition_json=active_crew_json,
                )
                st.write(narration)
                st.code(json.dumps(details.get("accepted", []), indent=2), language="json")
                st.code(json.dumps(details.get("rejected", []), indent=2), language="json")

    with tab_logs:
        require_ui_perm(db, "campaign.read")
        st.subheader("Turn Logs")
        turns = db.query(TurnLog).filter(TurnLog.campaign_id == campaign.id).order_by(TurnLog.id.desc()).limit(50).all()
        if not turns:
            st.info("No turns logged yet.")
        else:
            options = [f"id={t.id} | actor={t.actor} | {t.created_at}" for t in turns]
            selected = st.selectbox("Select turn", options)
            selected_id = int(selected.split("|")[0].split("=")[1].strip())
            turn = next(t for t in turns if t.id == selected_id)
            st.code(turn.user_input, language="text")
            st.write(turn.narration)
            st.code(json.dumps(turn.accepted_commands, indent=2), language="json")
            st.code(json.dumps(turn.rejected_commands, indent=2), language="json")
            try:
                parsed_raw = json.loads(turn.ai_raw_output)
            except json.JSONDecodeError:
                parsed_raw = {"raw_text": turn.ai_raw_output}
            st.code(json.dumps(parsed_raw.get("intent", {}), indent=2), language="json")
            st.code(json.dumps(parsed_raw, indent=2), language="json")
