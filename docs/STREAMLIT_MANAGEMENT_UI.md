# Streamlit Management UI

This page summarizes the main Streamlit pages and what each one is used for.

## Main Pages

- `Campaign Console`
  - inspect campaign state
  - manage campaign rules and agency rules
  - configure agent crew definition
  - run crew dry-run/apply
  - inspect turn logs and extracted intent JSON
- `Health`
  - run health checks for DB/LLM/streamlit/processes
- `LLM Management`
  - switch provider (`ollama`, `openai`, `stub`)
  - configure task models (narration/intent/review)
  - list/pull/delete Ollama models
  - list OpenAI-compatible models
  - save LLM settings to `.env`
- `Item Tracker`
  - view global item/effect knowledge and learned relevance
- `System Logs`
  - filter and inspect unified logs
- `Bot Manager`
  - add/edit/enable/disable/delete Discord bot configs
- `Users & Roles`
  - user/role/permission management and Discord linking
- `Admin`
  - DB connection test/save
- `Documentation`
  - renders markdown files from `docs/*.md`

## Permission Gating

When `AIGM_AUTH_ENFORCE=true`, pages are shown based on permissions.

Typical examples:

- `campaign.read` for campaign/item pages
- `system.admin` for health/logs/bot/admin/llm pages
- `user.manage` for users and roles
