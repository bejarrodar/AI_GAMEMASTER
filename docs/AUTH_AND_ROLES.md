# Auth and Roles

Authentication and RBAC are database-backed.

## Core Objects

- users
- roles
- permissions
- role-permission mappings
- user-role mappings

## Key Permissions

- `campaign.read`
- `campaign.play`
- `campaign.retry`
- `campaign.write`
- `campaign.import`
- `campaign.export`
- `rules.manage`
- `system.admin`
- `user.manage`

## Streamlit Administration

Use **Users & Roles** to:

- create users
- reset passwords
- assign roles
- link Discord user IDs
- create roles
- map permissions to roles

## Environment Controls

- `AIGM_AUTH_ENFORCE=true|false`
- `AIGM_AUTH_BOOTSTRAP_ADMIN_USERNAME`
- `AIGM_AUTH_BOOTSTRAP_ADMIN_PASSWORD`
- `AIGM_AUTH_REQUIRE_SYS_ADMIN_TOKEN=true|false`
- `AIGM_STREAMLIT_SESSION_TTL_S`
- `AIGM_BOT_ADMIN_SESSION_TTL_S`

When enforcement is enabled, UI page visibility and Discord operations are permission-gated.

## Security Tradeoffs

- The management API is fail-closed by default. If `AIGM_SYS_ADMIN_TOKEN` is missing and `AIGM_AUTH_REQUIRE_SYS_ADMIN_TOKEN=true`, privileged API routes reject requests instead of silently becoming public.
- `POST /api/v1/auth/login` is intentionally reachable without the sys-admin bearer token so Streamlit can bootstrap a user session. This is a deliberate usability tradeoff, and the route still goes through rate limiting.
- Streamlit authentication is session-based rather than token-based. That keeps the UI simple, but sessions now expire after `AIGM_STREAMLIT_SESSION_TTL_S` and should still be used behind HTTPS and trusted browser/session controls.
- Discord `!adminauth` remains a convenience path for bot administration, but it is no longer indefinite. Sessions expire after `AIGM_BOT_ADMIN_SESSION_TTL_S` to reduce risk if an operator walks away or a token is exposed.
- Bootstrap admin credentials are for first-run setup only. Leave `AIGM_AUTH_BOOTSTRAP_ADMIN_PASSWORD` blank outside controlled initialization, or inject it temporarily via a secret source.
