# AI GameMaster Docker Stacks

This repo now includes three standalone Compose stacks:

- `docker-compose.db.yml`: PostgreSQL only
- `docker-compose.web.yml`: Streamlit UI, management API, DB API, and health API
- `docker-compose.bot.yml`: Discord bot with its own HTTP health endpoint

Each stack has its own internal bridge network and also joins a shared edge network named `aigm-edge` by default. That shared edge network is what lets you place a reverse proxy or load balancer in front of the HTTP services while still deploying each stack separately.

## Quick start

1. Copy the example env files:

```bash
cp deploy/env/db.env.example deploy/env/db.env
cp deploy/env/web.env.example deploy/env/web.env
cp deploy/env/bot.env.example deploy/env/bot.env
```

2. Fill in real secrets and hostnames.

3. Start the stacks:

```bash
docker compose -f deploy/docker-compose.db.yml --env-file deploy/env/db.env up -d
docker compose -f deploy/docker-compose.web.yml --env-file deploy/env/web.env up -d
docker compose -f deploy/docker-compose.bot.yml --env-file deploy/env/bot.env up -d
```

## Service endpoints

- Web UI: `http://<host>:9531`
- Management API: `http://<host>:9541/api/v1/meta`
- DB API: `http://<host>:9542/db/v1/health`
- Health API: `http://<host>:9540/health`
- Bot health: `http://<host>:9550/health`

## Load balancer notes

- The web-facing HTTP services are safe to place behind a load balancer as long as the balancer forwards traffic to the exposed container ports and uses the provided health endpoints.
- The Discord bot is not a normal request/response web service. You can monitor or orchestrate it with the bot health endpoint, but you should not run multiple active replicas of the same Discord token behind a load balancer.
- The DB API and management API are operator surfaces, not public APIs. Put them on private networks or require authenticated access through your proxy/LB.
- The management API intentionally keeps the login route reachable without the sys-admin bearer token so the Streamlit UI can establish a session. All other privileged routes are expected to stay protected by `AIGM_SYS_ADMIN_TOKEN`.
