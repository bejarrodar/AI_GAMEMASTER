# Health and Logs

Use **Health** and **System Logs** pages for runtime visibility.

## Health

Health checks cover:

- database connectivity
- Ollama reachability
- Streamlit availability
- supervised process status (when using supervisor health endpoint)
- secret rotation recency (from `admin_audit_logs`)

Recommended endpoint:

- `AIGM_HEALTHCHECK_URL`
- metrics endpoint: `/metrics` (Prometheus text format)

## Logs

System logs are written to:

- filesystem log files in `AIGM_LOG_DIR`
- database table `system_logs` via DB API batch ingestion (`POST /db/v1/logs/system/batch`)

Log controls:

- `AIGM_LOG_FILE_MAX_BYTES`
- `AIGM_LOG_FILE_BACKUP_COUNT`
- `AIGM_LOG_DB_BATCH_SIZE`
- `AIGM_LOG_DB_FLUSH_INTERVAL_S`
- `AIGM_HEALTH_LOG_INTERVAL_S`
- `AIGM_HEALTH_ALERT_CONSECUTIVE_FAILURES`
- `AIGM_HEALTH_ALERT_WEBHOOK_URL`
- `AIGM_HEALTH_ALERT_WEBHOOK_COOLDOWN_S`
- `AIGM_SECRET_ROTATION_MAX_AGE_DAYS`
- `AIGM_ALERT_TURN_STALL_S`
- `AIGM_ALERT_TURN_STALL_QUEUE_DEPTH`
- `AIGM_ALERT_FALLBACK_WINDOW_S`
- `AIGM_ALERT_FALLBACK_THRESHOLD`
- `AIGM_ALERT_LATENCY_WINDOW_S`
- `AIGM_ALERT_LATENCY_THRESHOLD_MS`
- `AIGM_ALERT_LATENCY_BREACH_COUNT`
- `AIGM_ALERT_RUNTIME_COOLDOWN_S`

Use **System Logs** filters by service, level, time window, and message search.

Metrics currently include:

- health request/failure counters
- health snapshot duration metrics
- turn success/failure counters
- turn latency sum/count (for average computation)
- supervisor log queue depth gauge

Webhook alerting:

- When consecutive health failures reach threshold, supervisor emits an error log.
- If `AIGM_HEALTH_ALERT_WEBHOOK_URL` is configured, supervisor POSTs a JSON alert payload.
- Cooldown is controlled by `AIGM_HEALTH_ALERT_WEBHOOK_COOLDOWN_S` to prevent alert spam.
- Runtime alerts are also emitted for:
  - stalled turn processing (queue depth + no recent turn metrics)
  - repeated LLM fallback events
  - latency anomaly bursts (repeated high-latency turn successes)

## Admin Audit Trail

Administrative actions are written to `admin_audit_logs` with:

- actor source/id/display
- action and target
- metadata payload
- timestamp

In Streamlit, these are visible under **System Logs** in the **Admin Audit Logs** section.
