# SLO/SLI and Alert Routing

This document defines baseline service-level indicators (SLIs), service-level objectives (SLOs), and alert routing for the local/production stack.

## Scope

- Discord turn-processing path
- Management API
- DB API
- Supervisor health endpoint

## SLIs

1. Turn Success Rate
- Definition: successful turns / total turns.
- Source: `/metrics` counters (`aigm_turn_success_total`, `aigm_turn_failure_total`).

2. Turn Latency
- Definition: p95 end-to-end turn latency (message received -> narration emitted).
- Source: emitted turn latency samples and `/metrics` latency counters.

3. Health Availability
- Definition: successful `/health` snapshots over total snapshots.
- Source: supervisor health snapshots and `aigm_health_failures_total`.

4. Dependency Reachability
- Definition: DB/Ollama/Streamlit process checks pass percentage.
- Source: `/health` check payload fields.

## Initial SLO Targets

- Turn Success Rate: >= 99.0% over rolling 24h.
- Turn Latency (p95): <= 12s over rolling 1h (model-dependent; tune per deployment).
- Health Availability: >= 99.5% over rolling 24h.
- Dependency Reachability: >= 99.0% for DB and turn-LLM provider.

## Alert Routing

Primary route:
- Supervisor webhook (`AIGM_HEALTH_ALERT_WEBHOOK_URL`)

Recommended routing policy:
- `warning`: health failures below paging threshold -> team channel.
- `critical`: consecutive threshold reached or sustained SLO burn -> pager/on-call.

## Suggested Alert Rules

1. Health Failure Threshold
- Trigger: consecutive failures >= `AIGM_HEALTH_ALERT_CONSECUTIVE_FAILURES`.
- Existing behavior: supervisor emits alert event and optional webhook.

2. Turn Failure Burst
- Trigger: turn failure rate > 5% over 10 minutes.
- Action: page if sustained 15 minutes.

3. Latency Degradation
- Trigger: p95 turn latency > 2x baseline for 15 minutes.
- Action: warning -> critical escalation if sustained.

4. Fallback/Circuit-Breaker Spike
- Trigger: repeated provider fallback/circuit-open events.
- Action: warning; escalate if persistent > 30 minutes.

## Operational Notes

- Revisit thresholds per model/provider and hardware profile.
- Keep runbooks linked from alerts (restart paths, provider failover, queue pressure mitigation).
- Validate alerts during release drills and after major config/model changes.
