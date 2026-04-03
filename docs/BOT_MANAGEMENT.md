# Bot Management

Use the **Bot Manager** page in Streamlit to manage multiple Discord bots against one shared database.

## What You Can Do

- create bot configs
- update bot name/token/notes
- enable or disable each bot
- delete bot configs

## Runtime Behavior

- Enabled bot configs are launched by the bot manager process.
- If no enabled config exists, runtime can fall back to `AIGM_DISCORD_TOKEN` for compatibility.
- All bots share campaign/state tables in the same DB.

## Required Permissions

- Streamlit page visibility and edits require `system.admin`.

## Recommended Operations

- Keep one token per bot config.
- Disable bots before deleting configs.
- Rotate Discord tokens if shared accidentally.
