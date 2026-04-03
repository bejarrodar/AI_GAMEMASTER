from __future__ import annotations

import json
from urllib import error, parse, request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from sqlalchemy import create_engine

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional in stub or Ollama-only environments.
    OpenAI = None

from aigm.adapters.llm import LLMAdapter
from aigm.agents.crew import default_agent_crew_definition
from aigm.config import settings
from aigm.ops.db_api_client import DBApiClient
from aigm.services.game_service import GameService


st.set_page_config(page_title="AI GameMaster Console", layout="wide")
st.title("AI GameMaster Console")
st.caption("Manage campaigns, rules, agents, and admin operations.")

service = GameService(LLMAdapter())
db_api_client = DBApiClient(settings.db_api_url, token=settings.db_api_token, timeout_s=10)


def management_api_request(method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
    base = settings.management_api_url.rstrip("/")
    url = f"{base}{path}"
    if query:
        q = parse.urlencode({k: v for k, v in query.items() if v is not None and str(v) != ""})
        if q:
            url = f"{url}?{q}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = settings.sys_admin_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url=url, method=method.upper(), data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}
    except error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:  # noqa: BLE001
            payload = {}
        if "ok" not in payload:
            payload["ok"] = False
        if "message" not in payload and "error" not in payload:
            payload["message"] = f"HTTP {exc.code}: {exc.reason}"
        return payload


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


def current_auth_user() -> dict | None:
    user = st.session_state.get("auth_user")
    issued_at = st.session_state.get("auth_user_issued_at")
    if not isinstance(user, dict):
        return None
    try:
        issued_at_value = float(issued_at)
    except (TypeError, ValueError):
        st.session_state.pop("auth_user", None)
        st.session_state.pop("auth_user_issued_at", None)
        return None
    ttl_s = max(300, int(settings.streamlit_session_ttl_s))
    import time

    if (time.time() - issued_at_value) > ttl_s:
        st.session_state.pop("auth_user", None)
        st.session_state.pop("auth_user_issued_at", None)
        return None
    return user


def ui_has_perm(_db_unused, permission: str) -> bool:
    if not settings.auth_enforce:
        return True
    user = current_auth_user()
    if not user:
        return False
    perms = user.get("permissions", [])
    return permission in perms or "system.admin" in perms


def require_ui_perm(_db_unused, permission: str) -> None:
    if ui_has_perm(None, permission):
        return
    st.error(f"Permission denied: {permission}")
    st.stop()


def audit_ui_action(_db_unused, action: str, target: str = "", metadata: dict | None = None) -> None:
    _ = (action, target, metadata)
    return


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
    if OpenAI is None:
        return False, [], "OpenAI SDK is not installed."
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


db = None
if settings.auth_enforce:
    user = current_auth_user()
    if not user:
        st.subheader("Login")
        username = st.text_input("Username", key="login_username")
        password = st.text_input("Password", type="password", key="login_password")
        if st.button("Login", key="login_submit"):
            resp = management_api_request(
                "POST",
                "/api/v1/auth/login",
                payload={"username": username.strip(), "password": password},
            )
            auth_user = resp.get("user") if bool(resp.get("ok", False)) else None
            if auth_user:
                import time

                st.session_state["auth_user"] = auth_user
                st.session_state["auth_user_issued_at"] = time.time()
                st.success("Authenticated.")
                st.rerun()
            else:
                st.error(str(resp.get("message", "Invalid credentials.")))
        st.stop()

    st.sidebar.success(f"Logged in as {str(user.get('username', 'unknown'))}")
    if st.sidebar.button("Logout", key="sidebar_logout"):
        st.session_state.pop("auth_user", None)
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

if True:
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
        items = db_api_client.list_item_knowledge(limit=200)
        st.dataframe(
            [
                {
                    "item_key": i.get("item_key"),
                    "canonical_name": i.get("canonical_name"),
                    "type": i.get("object_type"),
                    "portability": i.get("portability"),
                    "rarity": i.get("rarity"),
                    "observations": i.get("observation_count"),
                    "confidence": round(float(i.get("confidence", 0.0) or 0.0), 3),
                    "summary": i.get("summary"),
                }
                for i in items
            ],
            width="stretch",
        )
        st.subheader("Global Learned Relevance")
        rel = db_api_client.list_item_relevance(limit=300)
        st.dataframe(
            [
                {
                    "item_key": r.get("item_key"),
                    "context_tag": r.get("context_tag"),
                    "interactions": r.get("interaction_count"),
                    "score": round(float(r.get("score", 0.0) or 0.0), 3),
                }
                for r in rel
            ],
            width="stretch",
        )
        st.subheader("Global Effect Knowledge")
        effects = db_api_client.list_effect_knowledge(limit=200)
        st.dataframe(
            [
                {
                    "effect_key": e.get("effect_key"),
                    "canonical_name": e.get("canonical_name"),
                    "category": e.get("category"),
                    "observations": e.get("observation_count"),
                    "confidence": round(float(e.get("confidence", 0.0) or 0.0), 3),
                    "summary": e.get("summary"),
                }
                for e in effects
            ],
            width="stretch",
        )
        st.subheader("Global Effect Relevance")
        erel = db_api_client.list_effect_relevance(limit=300)
        st.dataframe(
            [
                {
                    "effect_key": r.get("effect_key"),
                    "context_tag": r.get("context_tag"),
                    "interactions": r.get("interaction_count"),
                    "score": round(float(r.get("score", 0.0) or 0.0), 3),
                }
                for r in erel
            ],
            width="stretch",
        )
        st.stop()

    if page == "Gameplay & Knowledge":
        require_ui_perm(db, "campaign.read")
        st.subheader("Gameplay & Knowledge")

        campaigns = management_api_request("GET", "/api/v1/campaigns", query={"limit": 500}).get("rows", [])
        campaign_map = {
            f"{int(c.get('id', 0))} | {c.get('discord_thread_id', '')} | {c.get('mode', '')}": c for c in campaigns
        }
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
                active = management_api_request(
                    "GET",
                    f"/api/v1/campaigns/{int(selected_campaign['id'])}/ruleset",
                ).get("ruleset")
                st.write(f"Current ruleset: `{str((active or {}).get('key', 'none'))}`")
                options = management_api_request(
                    "GET",
                    "/api/v1/game/rulesets",
                    query={"enabled_only": "true"},
                ).get("rows", [])
                option_keys = [str(r.get("key", "")).strip() for r in options if str(r.get("key", "")).strip()]
                if option_keys:
                    pick = st.selectbox("Assign ruleset", option_keys, key="gk_assign_ruleset")
                    if st.button("Save campaign ruleset", key="gk_assign_save"):
                        require_ui_perm(db, "campaign.write")
                        resp = management_api_request(
                            "POST",
                            f"/api/v1/campaigns/{int(selected_campaign['id'])}/ruleset",
                            payload={"ruleset_key": pick},
                        )
                        if bool(resp.get("ok", False)):
                            audit_ui_action(
                                db,
                                action="campaign_ruleset_set",
                                target=f"{selected_campaign['id']}:{pick}",
                            )
                            st.success(f"Campaign ruleset set to `{pick}`.")
                            st.rerun()
                        else:
                            st.error(str(resp.get("message", "Failed to set ruleset.")))

        with tab_rulesets:
            rows = management_api_request("GET", "/api/v1/game/rulesets", query={"enabled_only": "false"}).get("rows", [])
            st.dataframe(
                [
                    {
                        "key": r.get("key"),
                        "name": r.get("name"),
                        "system": r.get("system"),
                        "version": r.get("version"),
                        "official": r.get("is_official"),
                        "enabled": r.get("is_enabled"),
                        "summary": r.get("summary"),
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
                    resp = management_api_request(
                        "POST",
                        "/api/v1/game/rulesets/upsert",
                        payload={
                            "key": rk,
                            "name": rn,
                            "system": rsys,
                            "version": rver,
                            "summary": rsum,
                            "is_official": roff,
                            "is_enabled": ren,
                            "rules_json": parsed_rules,
                        },
                    )
                    if bool(resp.get("ok", False)):
                        audit_ui_action(db, action="ruleset_upserted", target=rk)
                        st.success(f"Ruleset upserted: `{rk}`")
                        st.rerun()
                    else:
                        st.error(str(resp.get("message", "Failed to upsert ruleset.")))

        with tab_rulebooks:
            books = management_api_request("GET", "/api/v1/game/rulebooks", query={"enabled_only": "false"}).get("rows", [])
            st.dataframe(
                [
                    {
                        "slug": b.get("slug"),
                        "title": b.get("title"),
                        "system": b.get("system"),
                        "version": b.get("version"),
                        "source": b.get("source"),
                        "enabled": b.get("is_enabled"),
                        "summary": b.get("summary"),
                    }
                    for b in books
                ],
                width="stretch",
            )
            search_q = st.text_input("Lookup query", key="gk_rulelookup_query")
            if st.button("Search rulebook entries", key="gk_rulelookup_btn"):
                rows = management_api_request(
                    "GET",
                    "/api/v1/game/rulebooks/search",
                    query={"query": search_q, "limit": 6},
                ).get("rows", [])
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
                resp = management_api_request(
                    "POST",
                    "/api/v1/game/rulebooks/upsert",
                    payload={
                        "slug": b_slug,
                        "title": b_title,
                        "system": b_system,
                        "version": b_version,
                        "source": b_source,
                        "summary": b_summary,
                        "is_enabled": b_enabled,
                    },
                )
                if bool(resp.get("ok", False)):
                    audit_ui_action(db, action="rulebook_upserted", target=b_slug)
                    st.success(f"Rulebook upserted: `{b_slug}`")
                    st.rerun()
                else:
                    st.error(str(resp.get("message", "Failed to upsert rulebook.")))

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
                resp = management_api_request(
                    "POST",
                    "/api/v1/game/rulebooks/entries/upsert",
                    payload={
                        "rulebook_slug": e_book,
                        "entry_key": e_key,
                        "title": e_title,
                        "section": e_section,
                        "page_ref": e_page,
                        "tags": tags,
                        "content": e_content,
                    },
                )
                if bool(resp.get("ok", False)):
                    audit_ui_action(db, action="rulebook_entry_upserted", target=f"{e_book}:{e_key}")
                    st.success(f"Entry upserted: `{e_key}`")
                    st.rerun()
                else:
                    st.error(str(resp.get("message", "Failed to upsert entry.")))

        with tab_dice:
            expr = st.text_input("Dice expression", value="d20", key="gk_roll_expr")
            if st.button("Test roll", key="gk_roll_btn"):
                resp = management_api_request("POST", "/api/v1/dice/roll", payload={"expression": expr})
                if bool(resp.get("ok", False)):
                    st.code(json.dumps(resp.get("roll", {}), indent=2), language="json")
                else:
                    st.error(str(resp.get("roll", {}).get("error", "Invalid roll")))
            logs = db_api_client.list_dice_rolls(limit=100)
            st.dataframe(
                [
                    {
                        "id": r.get("id"),
                        "campaign_id": r.get("campaign_id"),
                        "actor": r.get("actor_display_name"),
                        "expression": r.get("expression"),
                        "normalized": r.get("normalized_expression"),
                        "total": r.get("total"),
                        "created_at": str(r.get("created_at")),
                    }
                    for r in logs
                ],
                width="stretch",
            )
        st.stop()

    if page == "System Logs":
        require_ui_perm(db, "system.admin")
        st.subheader("System Logs")
        try:
            seed_rows = management_api_request("GET", "/api/v1/logs/system", query={"limit": 500}).get("rows", [])
        except Exception:
            seed_rows = []
        service_options = ["all"] + sorted({str(r.get("service", "")).strip() for r in seed_rows if str(r.get("service", "")).strip()})
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

        try:
            api_rows = management_api_request(
                "GET",
                "/api/v1/logs/system",
                query={
                    "limit": int(row_limit),
                    "service": "" if service_filter == "all" else service_filter,
                    "level": "" if level_filter == "all" else level_filter,
                },
            ).get("rows", [])
        except Exception:
            api_rows = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(hours_back))
        rows = []
        for r in api_rows:
            created = str(r.get("created_at", "") or "")
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                created_dt = None
            if created_dt and created_dt < cutoff:
                continue
            msg = str(r.get("message", "") or "")
            if search_text.strip() and search_text.strip().lower() not in msg.lower():
                continue
            rows.append(r)
        st.dataframe(
            [
                {
                    "id": r.get("id"),
                    "created_at": str(r.get("created_at", "")),
                    "service": r.get("service"),
                    "level": r.get("level"),
                    "source": r.get("source"),
                    "message": r.get("message"),
                }
                for r in rows
            ],
            width="stretch",
        )
        st.subheader("Admin Audit Logs")
        audit_limit = st.number_input("Audit row limit", min_value=10, max_value=1000, value=200, step=10, key="audit_limit_top")
        try:
            audit_rows = management_api_request("GET", "/api/v1/logs/audit", query={"limit": int(audit_limit)}).get("rows", [])
        except Exception:
            audit_rows = []
        st.dataframe(
            [
                {
                    "id": r.get("id"),
                    "created_at": str(r.get("created_at", "")),
                    "actor_source": r.get("actor_source"),
                    "actor_id": r.get("actor_id"),
                    "actor_display": r.get("actor_display"),
                    "action": r.get("action"),
                    "target": r.get("target"),
                    "metadata": r.get("metadata"),
                }
                for r in audit_rows
            ],
            width="stretch",
        )
        st.stop()

    if page == "Bot Manager":
        require_ui_perm(db, "system.admin")
        st.subheader("Discord Bot Manager")
        try:
            bot_rows = management_api_request("GET", "/api/v1/bots").get("bots", [])
        except Exception:
            bot_rows = []
        st.dataframe(
            [
                {
                    "id": b.get("id"),
                    "name": b.get("name"),
                    "enabled": b.get("is_enabled"),
                    "notes": b.get("notes"),
                    "created_at": str(b.get("created_at", "")),
                    "updated_at": str(b.get("updated_at", "")),
                }
                for b in bot_rows
            ],
            width="stretch",
        )
        selected_bot_id = st.selectbox("Select bot config", [0] + [int(b.get("id", 0) or 0) for b in bot_rows], key="bot_cfg_pick_top")
        selected_bot = next((b for b in bot_rows if int(b.get("id", 0) or 0) == int(selected_bot_id)), None)
        bot_name = st.text_input("Bot name", value=str(selected_bot.get("name", "")) if selected_bot else "", key="bot_cfg_name_top")
        bot_token = st.text_input("Discord bot token", value="", type="password", key="bot_cfg_token_top")
        bot_enabled = st.checkbox("Enabled", value=bool(selected_bot.get("is_enabled", True)) if selected_bot else True, key="bot_cfg_enabled_top")
        bot_notes = st.text_area("Notes", value=str(selected_bot.get("notes", "")) if selected_bot else "", height=80, key="bot_cfg_notes_top")
        c_bot1, c_bot2, c_bot3 = st.columns(3)
        with c_bot1:
            if st.button("Save bot config", key="bot_cfg_save_top"):
                payload = {
                    "name": bot_name.strip(),
                    "discord_token": bot_token.strip() or str(selected_bot.get("discord_token", "")) if selected_bot else bot_token.strip(),
                    "is_enabled": bool(bot_enabled),
                    "notes": bot_notes.strip(),
                }
                if selected_bot:
                    management_api_request("PUT", f"/api/v1/bots/{int(selected_bot_id)}", payload=payload)
                else:
                    management_api_request("POST", "/api/v1/bots", payload=payload)
                audit_ui_action(
                    db,
                    action="bot_config_saved",
                    target=bot_name.strip() or (str(selected_bot.get("name", "")) if selected_bot else ""),
                    metadata={"selected_bot_id": int(selected_bot_id), "enabled": bool(bot_enabled)},
                )
                st.rerun()
        with c_bot2:
            if st.button("Toggle selected bot", key="bot_cfg_toggle_top") and selected_bot:
                new_enabled = not bool(selected_bot.get("is_enabled", False))
                management_api_request(
                    "PUT",
                    f"/api/v1/bots/{int(selected_bot_id)}",
                    payload={
                        "name": str(selected_bot.get("name", "")),
                        "discord_token": str(selected_bot.get("discord_token", "")),
                        "is_enabled": new_enabled,
                        "notes": str(selected_bot.get("notes", "")),
                    },
                )
                audit_ui_action(
                    db,
                    action="bot_config_toggled",
                    target=str(selected_bot.get("name", "")),
                    metadata={"bot_id": int(selected_bot_id), "enabled": new_enabled},
                )
                st.rerun()
        with c_bot3:
            if st.button("Delete selected bot", key="bot_cfg_delete_top") and selected_bot:
                deleted_name = str(selected_bot.get("name", ""))
                deleted_id = int(selected_bot_id)
                management_api_request("DELETE", f"/api/v1/bots/{deleted_id}")
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
        try:
            users_payload = management_api_request("GET", "/api/v1/auth/users")
            st.dataframe(users_payload.get("users", []), width="stretch")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed to load users from management API: {exc}")
        st.stop()

    if page == "Users & Roles":
        require_ui_perm(db, "user.manage")
        st.subheader("Users & Roles")

        try:
            users_payload = management_api_request("GET", "/api/v1/auth/users")
            roles_payload = management_api_request("GET", "/api/v1/auth/roles")
            perms_payload = management_api_request("GET", "/api/v1/auth/permissions")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed to load auth data from management API: {exc}")
            st.stop()

        users = users_payload.get("users", [])
        roles = roles_payload.get("roles", [])
        perms = perms_payload.get("permissions", [])
        role_names = [str(r.get("name", "")).strip() for r in roles if str(r.get("name", "")).strip()]
        perm_names = [str(p.get("name", "")).strip() for p in perms if str(p.get("name", "")).strip()]
        role_by_name = {str(r.get("name", "")).strip(): r for r in roles}

        st.markdown("### Users")
        st.dataframe(users, width="stretch")

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
            resp = management_api_request(
                "POST",
                "/api/v1/auth/users",
                payload={
                    "username": new_user.strip(),
                    "password": new_pwd,
                    "display_name": new_display.strip(),
                    "roles": new_roles or ["player"],
                },
            )
            ok = bool(resp.get("ok", False))
            detail = str(resp.get("message", ""))
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
                encoded = parse.quote(m_user.strip(), safe="")
                resp = management_api_request(
                    "POST",
                    f"/api/v1/auth/users/{encoded}/roles",
                    payload={"role": assign_role},
                )
                ok = bool(resp.get("ok", False))
                detail = str(resp.get("message", ""))
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
                encoded = parse.quote(m_user.strip(), safe="")
                resp = management_api_request(
                    "POST",
                    f"/api/v1/auth/users/{encoded}/password",
                    payload={"password": reset_pwd},
                )
                ok = bool(resp.get("ok", False))
                detail = str(resp.get("message", ""))
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
                encoded = parse.quote(m_user.strip(), safe="")
                resp = management_api_request(
                    "POST",
                    f"/api/v1/auth/users/{encoded}/discord",
                    payload={"discord_user_id": discord_id.strip()},
                )
                ok = bool(resp.get("ok", False))
                detail = str(resp.get("message", ""))
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
        st.dataframe(roles, width="stretch")

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
            else:
                resp = management_api_request(
                    "POST",
                    "/api/v1/auth/roles",
                    payload={"name": role_name, "description": new_role_desc.strip(), "permissions": new_role_perms},
                )
                if bool(resp.get("ok", False)):
                    audit_ui_action(
                        db,
                        action="auth_role_created",
                        target=role_name,
                        metadata={"permissions": new_role_perms},
                    )
                    st.success(str(resp.get("message", "Role created.")))
                    st.rerun()
                else:
                    st.error(str(resp.get("message", "Failed to create role.")))

        st.markdown("### Edit Role Permissions")
        selected_role_name = st.selectbox("Role", role_names, key="roles_edit_pick")
        selected_role = role_by_name.get(selected_role_name)
        if selected_role:
            current_perm_names = [str(x).strip() for x in selected_role.get("permissions", []) if str(x).strip()]
            desired_perms = st.multiselect("Permissions", perm_names, default=current_perm_names, key="roles_edit_perms")
            if st.button("Save role permissions", key="roles_edit_save"):
                encoded_role = parse.quote(selected_role_name.strip(), safe="")
                resp = management_api_request(
                    "PUT",
                    f"/api/v1/auth/roles/{encoded_role}/permissions",
                    payload={"permissions": desired_perms},
                )
                if bool(resp.get("ok", False)):
                    audit_ui_action(
                        db,
                        action="auth_role_permissions_updated",
                        target=selected_role_name,
                        metadata={"permissions": desired_perms},
                    )
                    st.success(str(resp.get("message", "Role permissions updated.")))
                    st.rerun()
                else:
                    st.error(str(resp.get("message", "Failed to update role permissions.")))
        st.stop()

    campaigns = db_api_client.list_campaigns(limit=500)
    if not campaigns:
        st.warning("No campaigns found yet. Start a Discord thread first so a campaign row exists.")
        st.stop()

    campaign_lookup: dict[str, int] = {}
    for c in campaigns:
        campaign_id = int(c.get("id", 0) or 0)
        if campaign_id <= 0:
            continue
        campaign_rules = db_api_client.campaign_rules(campaign_id)
        thread_name = campaign_rules.get("thread_name", "").strip()
        if not thread_name:
            resolved_name = resolve_thread_name_from_discord(str(c.get("discord_thread_id", "")))
            if resolved_name:
                thread_name = resolved_name
                db_api_client.set_campaign_rule(campaign_id, "thread_name", resolved_name)
        label = (
            f"{thread_name if thread_name else '(unknown thread name)'} | {c.get('discord_thread_id', '')} "
            f"(id={campaign_id}, mode={c.get('mode', '')})"
        )
        campaign_lookup[label] = campaign_id

selected_label = st.selectbox("Campaign", list(campaign_lookup.keys()))
campaign_id = campaign_lookup[selected_label]

campaign = db_api_client.campaign_by_id(int(campaign_id))
if not campaign:
    st.error("Campaign not found.")
    st.stop()
rules = db_api_client.campaign_rules(int(campaign["id"]))

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
        st.code(json.dumps(campaign.get("state", {}), indent=2), language="json")

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
                db_api_client.set_campaign_rule(int(campaign["id"]), rule_key.strip(), rule_value)
                audit_ui_action(
                    db,
                    action="campaign_rule_saved",
                    target=f"{campaign['id']}:{rule_key.strip()}",
                )
                st.success(f"Saved rule: {rule_key.strip()}")
                st.rerun()

with tab_agency:
        require_ui_perm(db, "campaign.read")
        st.subheader("Agency Rule Blocks")
        try:
            blocks = management_api_request("GET", "/api/v1/agency/rules").get("rows", [])
        except Exception as exc:  # noqa: BLE001
            st.error(f"Failed to load agency rules: {exc}")
            blocks = []
        blocks_by_id = {str(b.get("rule_id", "")): b for b in blocks}
        st.dataframe(
            [
                {
                    "rule_id": b.get("rule_id"),
                    "title": b.get("title"),
                    "priority": b.get("priority"),
                    "enabled": b.get("is_enabled"),
                    "body": b.get("body"),
                }
                for b in blocks
            ],
            width="stretch",
        )
        selected_rule_id = st.selectbox("Select rule to edit", [""] + [str(b.get("rule_id", "")) for b in blocks], key="agency_pick")
        selected_block = blocks_by_id.get(selected_rule_id)
        agency_rule_id = st.text_input("Rule ID", value=str(selected_block.get("rule_id", "")) if selected_block else "", key="agency_rule_id")
        agency_title = st.text_input("Title", value=str(selected_block.get("title", "")) if selected_block else "", key="agency_title")
        agency_priority = st.selectbox(
            "Priority",
            ["critical", "high", "medium", "low"],
            index=["critical", "high", "medium", "low"].index(str(selected_block.get("priority", "high"))) if selected_block else 1,
            key="agency_priority",
        )
        agency_body = st.text_area("Body", value=str(selected_block.get("body", "")) if selected_block else "", height=220, key="agency_body")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Upsert agency rule", key="agency_upsert"):
                require_ui_perm(db, "rules.manage")
                resp = management_api_request(
                    "POST",
                    "/api/v1/agency/rules/upsert",
                    payload={
                        "rule_id": agency_rule_id.strip(),
                        "title": agency_title.strip(),
                        "priority": agency_priority,
                        "body": agency_body.strip(),
                    },
                )
                if bool(resp.get("ok", False)):
                    audit_ui_action(
                        db,
                        action="agency_rule_upserted",
                        target=agency_rule_id.strip(),
                        metadata={"priority": agency_priority},
                    )
                    st.success(str(resp.get("message", "Upserted.")))
                    st.rerun()
                else:
                    st.error(str(resp.get("message", "Failed to upsert rule.")))
        with c2:
            if st.button("Remove selected rule", key="agency_remove"):
                require_ui_perm(db, "rules.manage")
                if not selected_block:
                    st.error("Pick an existing rule to remove.")
                else:
                    rid = str(selected_block.get("rule_id", "")).strip()
                    resp = management_api_request("DELETE", f"/api/v1/agency/rules/{parse.quote(rid, safe='')}")
                    if bool(resp.get("ok", False)):
                        audit_ui_action(
                            db,
                            action="agency_rule_removed",
                            target=rid,
                        )
                        st.success(str(resp.get("message", "Removed.")))
                        st.rerun()
                    else:
                        st.error(str(resp.get("message", "Failed to remove rule.")))
        with c3:
            if st.button("Toggle selected rule", key="agency_toggle"):
                require_ui_perm(db, "rules.manage")
                if not selected_block:
                    st.error("Pick an existing rule.")
                else:
                    rid = str(selected_block.get("rule_id", "")).strip()
                    new_enabled = not bool(selected_block.get("is_enabled", False))
                    resp = management_api_request(
                        "PUT",
                        f"/api/v1/agency/rules/{parse.quote(rid, safe='')}/enabled",
                        payload={"is_enabled": new_enabled},
                    )
                    if bool(resp.get("ok", False)):
                        audit_ui_action(
                            db,
                            action="agency_rule_toggled",
                            target=rid,
                            metadata={"enabled": new_enabled},
                        )
                        st.success(str(resp.get("message", "Updated.")))
                        st.rerun()
                    else:
                        st.error(str(resp.get("message", "Failed to update rule state.")))

with tab_agents:
        require_ui_perm(db, "campaign.read")
        st.subheader("Crew Definition")
        current_engine = str(rules.get("turn_engine", "classic")).strip().lower() or "classic"
        engine_choice = st.radio(
            "Turn engine",
            ["classic", "crew"],
            index=0 if current_engine == "classic" else 1,
            horizontal=True,
            key="turn_engine_choice",
        )
        if st.button("Save turn engine", key="save_turn_engine"):
            require_ui_perm(db, "campaign.write")
            db_api_client.set_campaign_rule(int(campaign["id"]), "turn_engine", engine_choice)
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
                    db_api_client.set_campaign_rule(int(campaign["id"]), "agent_crew_definition", crew_json)
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
                preview = management_api_request(
                    "POST",
                    f"/api/v1/campaigns/{int(campaign['id'])}/crew/preview",
                    payload={
                        "actor": actor_id.strip(),
                        "user_input": user_input.strip(),
                        "crew_definition_json": active_crew_json,
                    },
                )
                if bool(preview.get("ok", False)):
                    st.write(preview["narration"])
                    st.code(json.dumps(preview["accepted"], indent=2), language="json")
                    st.code(json.dumps(preview["rejected"], indent=2), language="json")
                    st.code(json.dumps(preview["crew_outputs"], indent=2), language="json")
                else:
                    st.error(str(preview.get("message") or preview.get("error") or "Failed to preview crew turn."))
        with c2:
            if st.button("Apply crew turn", key="crew_apply"):
                result = management_api_request(
                    "POST",
                    f"/api/v1/campaigns/{int(campaign['id'])}/crew/apply",
                    payload={
                        "actor": actor_id.strip(),
                        "actor_display_name": actor_name.strip() or actor_id.strip(),
                        "user_input": user_input.strip(),
                        "crew_definition_json": active_crew_json,
                    },
                )
                if not bool(result.get("ok", False)):
                    st.error(str(result.get("message") or result.get("error") or "Failed to apply crew turn."))
                else:
                    narration = str(result.get("narration", ""))
                    details = result.get("details", {})
                    st.write(narration)
                    st.code(json.dumps(details.get("accepted", []), indent=2), language="json")
                    st.code(json.dumps(details.get("rejected", []), indent=2), language="json")

with tab_logs:
        require_ui_perm(db, "campaign.read")
        st.subheader("Turn Logs")
        turns = db_api_client.list_turn_logs(campaign_id=int(campaign["id"]), limit=50)
        turn_objs = [type("TurnRow", (), t) for t in turns]
        if not turn_objs:
            st.info("No turns logged yet.")
        else:
            options = [f"id={t.id} | actor={t.actor} | {t.created_at}" for t in turn_objs]
            selected = st.selectbox("Select turn", options)
            selected_id = int(selected.split("|")[0].split("=")[1].strip())
            turn = next(t for t in turn_objs if int(t.id) == selected_id)
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
