# HA / DR Runbook

This runbook documents active-passive recovery guidance for AI GameMaster deployments.

## Reference Topology

- application node(s): supervisor + streamlit + bot manager
- primary database: PostgreSQL
- standby database: replicated read replica / warm standby
- shared secrets source for failover target

## Failure Classes

- app process failure
- host failure
- DB primary failure
- region/AZ disruption

## App Process Recovery

1. Check service status (`aigm-supervisor`).
2. Inspect health endpoint and `/metrics`.
3. Restart supervisor service.
4. Verify bot manager + streamlit child processes are healthy.

## DB Primary Failure (Active-Passive)

1. Confirm primary DB outage.
2. Promote standby DB to primary using platform runbook.
3. Update secret source / `AIGM_DATABASE_URL` target for app runtime.
4. Restart app supervisors to refresh DB connections.
5. Verify:
   - `/health` returns DB reachable
   - Discord turns process successfully
   - migration/bootstraps are not reinitializing unexpectedly

## Restore from Backup

1. Stop app services.
2. Restore latest backup:
   - encrypted: `scripts/restore_db.py <backup>.enc --encrypted`
   - plain: `scripts/restore_db.py <backup>`
3. Run smoke checks and health endpoint checks.
4. Resume app services.

## RPO / RTO Drill Template

- RPO target: _____ minutes
- RTO target: _____ minutes
- Last drill date: _____
- Drill result: pass / fail
- Notes and corrective actions: _____
