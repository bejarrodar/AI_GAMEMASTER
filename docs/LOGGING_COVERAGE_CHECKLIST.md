# Logging Coverage Checklist

Use this checklist when validating logging completeness for releases.

## Startup

- [ ] Supervisor startup logged (`supervisor` service, INFO).
- [ ] DB API startup logged with bind/port.
- [ ] Management API startup logged with bind/port.
- [ ] Streamlit/bot manager child process start events logged.
- [ ] Secret source access/rotation events audited when configured.

## Request Handling

- [ ] Management API request failures logged with method/path/error.
- [ ] DB API request failures logged with method/path/error.
- [ ] Unauthorized API access attempts logged or counted.
- [ ] Rate-limited API requests logged with scope (`management_api`, `management_api_mutation`).

## Gameplay / Turn Processing

- [ ] Turn start and completion include a correlation ID.
- [ ] Turn failures include rollback envelope details.
- [ ] Rejected commands are persisted and visible in turn logs.
- [ ] Queue-full turn rejections are logged and surfaced to users.

## LLM Reliability

- [ ] Provider failures logged with provider name and exception class.
- [ ] Circuit-breaker open/close behavior is observable in logs.
- [ ] Fallback path usage is logged/counted for monitoring.
- [ ] Slow provider responses produce visible timeout/failure signals.

## Data Mutations

- [ ] Campaign rule mutations logged/audited.
- [ ] Agency rule CRUD and enable/disable actions audited.
- [ ] Bot config CRUD operations audited.
- [ ] Auth user/role/permission mutations audited.

## Health / Alerts

- [ ] Periodic health snapshots are logged at configured interval.
- [ ] Consecutive health-failure threshold alerts are logged.
- [ ] Health recovery event after alert is logged.
- [ ] Webhook delivery success/failure is logged when configured.

## Operations

- [ ] Tracebacks are coalesced into multiline single log records.
- [ ] File rotation and retention are functioning (`AIGM_LOG_DIR`).
- [ ] DB log batching is active under load.
- [ ] `/metrics` exposes turn counters/latency and queue depth.
