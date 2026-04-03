# Structured Log Schema (Baseline)

All runtime component logs written by supervisor follow JSON schema `aigm.log.v1`.

## Schema: `aigm.log.v1`

Required fields:

- `schema_version` (string): fixed `aigm.log.v1`
- `ts` (string): UTC ISO timestamp
- `service` (string): logical source (`supervisor`, `bot_manager`, `streamlit`, `health`, `api`, etc.)
- `level` (string): `DEBUG|INFO|WARNING|ERROR`
- `message` (string): human-readable event text
- `source` (string): producer path (`runtime`, `subprocess`, `management_api`, etc.)
- `metadata` (object): structured event details

Example:

```json
{
  "schema_version": "aigm.log.v1",
  "ts": "2026-02-16T18:12:00.123456+00:00",
  "service": "health",
  "level": "WARNING",
  "message": "Health failure alert threshold reached.",
  "source": "runtime",
  "metadata": {
    "event": "aigm_health_alert",
    "consecutive_failures": 3,
    "alert_threshold": 3
  }
}
```

## Persistence Path

- File sinks: `AIGM_LOG_DIR` (`combined.log` + per-service logs)
- DB sink: via DB API endpoint `/db/v1/logs/system/batch`

## Compatibility Contract

- New fields may be added in `metadata` without version bump.
- Breaking top-level schema changes require version bump (`aigm.log.v2`).
- Consumers should tolerate unknown `metadata` keys.
