# Deployment Hardening

This checklist covers production hardening for network exposure and operations.

## Network and TLS

- place Streamlit and health endpoints behind a reverse proxy
- enforce HTTPS/TLS termination at proxy/load balancer
- restrict direct public access to internal service ports
- allow only required ingress ports (typically `443`)
- use outbound egress controls where possible

## Service Isolation

- run app services under non-admin service accounts
- separate DB host/network where feasible
- segment bot runtime from admin UI runtime in strict environments
- avoid running development/debug tooling in production hosts

## Secrets and Credentials

- prefer secret manager or file-mounted secrets over plaintext `.env`
- supported runtime sources include:
  - `AIGM_SECRET_SOURCE=aws_secrets_manager` (AWS CLI-backed retrieval)
  - `AIGM_SECRET_SOURCE=command` (custom command emits JSON secrets)
  - `AIGM_SECRET_SOURCE=json_file` (mounted JSON secret file)
- rotate Discord/OpenAI/admin tokens regularly
- grant least privilege on DB credentials
- monitor for leaked credentials and revoke quickly

## Observability and Alerts

- scrape `/metrics` and configure alert rules
- configure `AIGM_HEALTH_ALERT_WEBHOOK_URL` for failure notifications
- retain system and audit logs per policy
- validate backup and restore procedures on schedule

## Reverse Proxy Notes

- ensure proxy preserves client IP (if needed for auditing)
- configure request body and timeout limits aligned with expected LLM latency
- add basic WAF/rate limit policies at edge/proxy layer
