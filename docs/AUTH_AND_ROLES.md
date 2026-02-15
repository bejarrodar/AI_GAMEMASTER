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

When enforcement is enabled, UI page visibility and Discord operations are permission-gated.
