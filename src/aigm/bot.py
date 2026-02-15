from __future__ import annotations

import discord
from discord.ext import commands

from aigm.adapters.llm import LLMAdapter
from aigm.config import settings
from aigm.core.rules import merge_rules
from aigm.db.init_db import init_db
from aigm.db.session import SessionLocal
from aigm.schemas.game import Command
from aigm.services.game_service import GameService


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
service = GameService(LLMAdapter())
authenticated_admin_ids: set[str] = set()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if message.channel.type not in (discord.ChannelType.public_thread, discord.ChannelType.private_thread):
        return

    with SessionLocal() as db:
        campaign = service.get_or_create_campaign(db, thread_id=str(message.channel.id), mode="dnd")
        player = service.ensure_player(db, campaign, str(message.author.id), message.author.display_name)
        actor_id = str(message.author.id)

        if message.content.startswith("!adminauth "):
            token = message.content.removeprefix("!adminauth ").strip()
            ok = service.authenticate_sys_admin(db, actor_id, message.author.display_name, token)
            if ok:
                authenticated_admin_ids.add(actor_id)
                await message.channel.send("Sys admin authentication successful.")
            else:
                await message.channel.send("Sys admin authentication failed.")
            return

        if message.content.startswith("!adminlogout"):
            authenticated_admin_ids.discard(actor_id)
            await message.channel.send("Sys admin session cleared.")
            return

        is_admin = actor_id in authenticated_admin_ids and service.is_sys_admin(db, actor_id)

        if message.content.startswith("!adminrules"):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            rows = service.admin_list_rule_blocks(db)
            rendered = "\n".join(f"- {r.rule_id} | {r.priority} | enabled={r.is_enabled}" for r in rows)
            await message.channel.send(f"System agency rules:\n{rendered[:1800]}")
            return

        if message.content.startswith("!adminrule add "):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            payload = message.content.removeprefix("!adminrule add ").strip()
            parts = [p.strip() for p in payload.split("|", 3)]
            if len(parts) != 4:
                await message.channel.send("Usage: !adminrule add <rule_id>|<title>|<priority>|<body>")
                return
            rule_id, title, priority, body = parts
            service.admin_upsert_rule_block(db, rule_id=rule_id, title=title, priority=priority, body=body)
            await message.channel.send(f"Admin rule upserted: {rule_id}")
            return

        if message.content.startswith("!adminrule update "):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            payload = message.content.removeprefix("!adminrule update ").strip()
            parts = [p.strip() for p in payload.split("|", 3)]
            if len(parts) != 4:
                await message.channel.send("Usage: !adminrule update <rule_id>|<title>|<priority>|<body>")
                return
            rule_id, title, priority, body = parts
            service.admin_upsert_rule_block(db, rule_id=rule_id, title=title, priority=priority, body=body)
            await message.channel.send(f"Admin rule updated: {rule_id}")
            return

        if message.content.startswith("!adminrule remove "):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            rule_id = message.content.removeprefix("!adminrule remove ").strip()
            ok = service.admin_remove_rule_block(db, rule_id)
            await message.channel.send("Rule removed." if ok else "Rule not found.")
            return

        if message.content.startswith("!setrule "):
            payload = message.content.removeprefix("!setrule ").strip()
            if "=" not in payload:
                await message.channel.send("Usage: !setrule key=value")
                return
            key, value = payload.split("=", 1)
            service.set_rule(db, campaign, key.strip(), value.strip())
            await message.channel.send(f"Rule '{key.strip()}' updated.")
            return

        if message.content.startswith("!agencyprofiles"):
            profiles = ", ".join(service.available_rule_profiles())
            await message.channel.send(f"Agency rule profiles: {profiles}")
            return

        if message.content.startswith("!setagencyprofile "):
            profile = message.content.removeprefix("!setagencyprofile ").strip().lower()
            if profile not in service.available_rule_profiles():
                await message.channel.send("Unknown profile. Use !agencyprofiles")
                return
            service.set_rule(db, campaign, "agency_rule_profile", profile)
            service.set_rule(db, campaign, "agency_rule_ids", "")
            await message.channel.send(f"Agency profile set to '{profile}'.")
            return

        if message.content.startswith("!agencyrules"):
            rules = "\n".join(f"- {rid}" for rid in service.available_rule_ids(db))
            await message.channel.send(f"Available agency rule IDs:\n{rules}")
            return

        if message.content.startswith("!setagencyrules "):
            csv_ids = message.content.removeprefix("!setagencyrules ").strip()
            selected = [x.strip() for x in csv_ids.split(",") if x.strip()]
            invalid = [x for x in selected if x not in service.available_rule_ids(db)]
            if invalid:
                await message.channel.send(f"Invalid rule IDs: {', '.join(invalid)}")
                return
            service.set_rule(db, campaign, "agency_rule_ids", ",".join(selected))
            await message.channel.send("Custom agency rule selection saved.")
            return

        if message.content.startswith("!setcharacter "):
            value = message.content.removeprefix("!setcharacter ").strip()
            if not value:
                await message.channel.send("Usage: !setcharacter <character instructions>")
                return
            service.set_rule(db, campaign, "character_instructions", value)
            await message.channel.send("Character instructions updated for this campaign.")
            return

        if message.content.startswith("!setprompt "):
            value = message.content.removeprefix("!setprompt ").strip()
            if not value:
                await message.channel.send("Usage: !setprompt <custom campaign directives>")
                return
            service.set_rule(db, campaign, "system_prompt_custom", value)
            await message.channel.send("Custom system prompt directives updated.")
            return

        if message.content.startswith("!showprompt"):
            prompt_text = service.build_campaign_system_prompt(db, campaign)
            if len(prompt_text) > 1800:
                prompt_text = prompt_text[:1800] + "\n...[truncated]"
            await message.channel.send(f"```\n{prompt_text}\n```")
            return

        if message.content.startswith("!rules"):
            custom = service.list_rules(db, campaign)
            effective = merge_rules(custom)
            rendered = "\n".join(f"- {k}: {v}" for k, v in effective.items())
            await message.channel.send(f"Effective rules:\n{rendered}")
            return

        if message.content.startswith("!mycharacter "):
            description = message.content.removeprefix("!mycharacter ").strip()
            if not description:
                await message.channel.send("Usage: !mycharacter <natural language character description>")
                return
            char_name = service.register_character_from_description(db, campaign, player, description)
            await message.channel.send(f"Character linked to you: **{char_name}**")
            return

        if message.content.startswith("!additem "):
            parts = message.content.split()
            if len(parts) != 4:
                await message.channel.send("Usage: !additem <character_name> <item_key> <quantity>")
                return
            _, character_name, item_key, quantity_text = parts
            try:
                quantity = int(quantity_text)
            except ValueError:
                await message.channel.send("Quantity must be an integer.")
                return
            ok, detail = service.apply_manual_commands(
                db,
                campaign,
                actor=actor_id,
                user_input=message.content,
                commands=[Command(type="add_item", target=character_name, key=item_key, amount=quantity)],
            )
            await message.channel.send("Item added." if ok else f"Failed to add item: {detail}")
            return

        if message.content.startswith("!addeffect "):
            payload = message.content.removeprefix("!addeffect ").strip()
            parts = [p.strip() for p in payload.split("|", 4)]
            if len(parts) != 5:
                await message.channel.send(
                    "Usage: !addeffect <character>|<magical|physical|misc>|<effect_key>|<duration_or_none>|<description>"
                )
                return
            character_name, category, effect_key, duration_text, description = parts
            try:
                duration = None if duration_text.lower() == "none" else int(duration_text)
            except ValueError:
                await message.channel.send("Duration must be an integer or none.")
                return
            ok, detail = service.apply_manual_commands(
                db,
                campaign,
                actor=actor_id,
                user_input=message.content,
                commands=[
                    Command(
                        type="add_effect",
                        target=character_name,
                        key=effect_key,
                        effect_category=category,
                        duration_turns=duration,
                        text=description,
                    )
                ],
            )
            await message.channel.send("Effect applied." if ok else f"Failed to apply effect: {detail}")
            return

        narration, details = service.process_turn(
            db,
            campaign=campaign,
            actor=actor_id,
            actor_display_name=message.author.display_name,
            user_input=message.content,
        )

    diagnostics = ""
    if details["rejected"]:
        diagnostics = f"\n\nRejected commands: {len(details['rejected'])}"

    await message.channel.send(f"{narration}{diagnostics}")


if __name__ == "__main__":
    init_db()
    if not settings.discord_token:
        raise RuntimeError("Set AIGM_DISCORD_TOKEN in environment")
    bot.run(settings.discord_token)
