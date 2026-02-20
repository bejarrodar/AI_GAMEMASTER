from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord
from discord.ext import commands

from aigm.adapters.llm import LLMAdapter
from aigm.config import settings
from aigm.core.rules import merge_rules
from aigm.db.models import Campaign, TurnLog
from aigm.db.init_db import init_db
from aigm.db.session import SessionLocal
from aigm.schemas.game import Command
from aigm.services.game_service import GameService


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
service = GameService(LLMAdapter())
authenticated_admin_ids: set[str] = set()
turn_job_queue: asyncio.Queue | None = None
turn_workers_started = False


@dataclass
class TurnJob:
    channel: discord.abc.Messageable
    campaign_id: int
    actor_id: str
    actor_display_name: str
    user_input: str


def _as_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _campaign_started(db, campaign) -> bool:
    rules = service.list_rules(db, campaign)
    if "game_started" in rules:
        return _as_truthy(str(rules.get("game_started", "")))
    return (
        db.query(TurnLog)
        .filter(TurnLog.campaign_id == campaign.id)
        .order_by(TurnLog.id.desc())
        .limit(1)
        .one_or_none()
        is not None
    )


def _possible_commands() -> list[str]:
    return [
        "!gmhelp",
        "!ping",
        "!startgame",
        "!startstory",
        "!retry",
        "!exportcampaign",
        "!importcampaign",
        "!adminrestorecampaign",
        "!adminauth",
        "!adminlogout",
        "!adminrules",
        "!adminrule",
        "!setrule",
        "!agencyprofiles",
        "!setagencyprofile",
        "!agencyrules",
        "!setagencyrules",
        "!setcharacter",
        "!setprompt",
        "!showprompt",
        "!rules",
        "!showruleset",
        "!setruleset",
        "!rulelookup",
        "!roll",
        "!agentmode",
        "!mycharacter",
        "!additem",
        "!addeffect",
        "!deletecharacter",
        "!teach",
        "!reportissue",
    ]


def _is_admin_command(cmd_name: str, content: str) -> bool:
    if cmd_name in {"!adminauth", "!adminlogout", "!adminrules", "!adminrestorecampaign"}:
        return True
    return content.startswith("!adminrule ")


async def _send_gm_help(channel: discord.abc.Messageable) -> None:
    help_text = (
        "**GameMaster Commands**\n"
        "- `!gmhelp`: Show command help.\n"
        "- `!ping`: Bot health ping.\n"
        "- `!startgame [dnd|story]`: Start/enable gameplay in this thread.\n"
        "- `!startstory`: Shortcut for `!startgame story`.\n"
        "- `!retry`: Retry your last input from pre-turn snapshot.\n"
        "- `!mycharacter <description>`\n"
        "- `!deletecharacter`: Delete your linked character.\n"
        "- `!roll <dice_expression>`\n"
        "- `!showruleset` / `!setruleset <key>` / `!rulelookup <query>`\n"
        "- `!agentmode [classic|crew]`: Set or view turn engine.\n"
        "- `!teach <item|effect>|<key>|<observation>`\n"
        "- `!reportissue <what went wrong>`: Log an issue report with recent context.\n"
        "- `!exportcampaign [scope]`: Create snapshot (`all|state|rules|players|characters|turns`).\n"
        "- `!importcampaign <snapshot_key>`\n"
        "- `!adminrestorecampaign <snapshot_key>` (admin)\n"
        "- `!adminauth <token>` / `!adminlogout`\n"
    )
    await channel.send(help_text[:1900])


async def _turn_worker() -> None:
    global turn_job_queue
    assert turn_job_queue is not None
    while True:
        job = await turn_job_queue.get()
        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(_typing_indicator_loop(job.channel, typing_stop, interval_s=8.0))
        try:
            narration, details = await asyncio.to_thread(_process_turn_sync, job)
            diagnostics = ""
            if details.get("rejected"):
                diagnostics = f"\n\nRejected commands: {len(details['rejected'])}"
            await job.channel.send(f"{narration}{diagnostics}")
        except Exception as exc:  # noqa: BLE001
            await job.channel.send(f"Turn processing failed: {exc}")
        finally:
            typing_stop.set()
            try:
                await typing_task
            except Exception:
                pass
            turn_job_queue.task_done()


async def _typing_indicator_loop(channel: discord.abc.Messageable, stop_event: asyncio.Event, interval_s: float = 8.0) -> None:
    delay = max(2.0, float(interval_s))
    while not stop_event.is_set():
        try:
            trigger_typing = getattr(channel, "trigger_typing", None)
            if callable(trigger_typing):
                await trigger_typing()
        except Exception:
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            continue


def _process_turn_sync(job: TurnJob) -> tuple[str, dict]:
    with SessionLocal() as db:
        campaign = db.query(Campaign).filter(Campaign.id == int(job.campaign_id)).one_or_none()
        if campaign is None:
            return "Campaign no longer exists for this thread.", {"accepted": [], "rejected": []}
        return service.process_turn_routed(
            db,
            campaign=campaign,
            actor=job.actor_id,
            actor_display_name=job.actor_display_name,
            user_input=job.user_input,
        )


@bot.event
async def on_ready() -> None:
    global turn_job_queue, turn_workers_started
    print(f"Logged in as {bot.user}")
    if turn_job_queue is None:
        turn_job_queue = asyncio.Queue(maxsize=max(1, int(settings.turn_worker_queue_max)))
    if not turn_workers_started:
        for _ in range(max(1, int(settings.turn_worker_count))):
            asyncio.create_task(_turn_worker())
        turn_workers_started = True
        print(
            f"[turn-workers] started workers={max(1, int(settings.turn_worker_count))} "
            f"queue_max={max(1, int(settings.turn_worker_queue_max))}"
        )
    # Best-effort sweep so the bot is a member of active threads.
    for guild in bot.guilds:
        for thread in guild.threads:
            try:
                if thread.archived:
                    continue
                if thread.me is None:
                    await thread.join()
                    print(f"[thread-join] joined existing thread {thread.id} in guild {guild.id}")
            except discord.Forbidden:
                print(f"[thread-join] missing permission for thread {thread.id} in guild {guild.id}")
            except discord.HTTPException as exc:
                print(f"[thread-join] failed joining thread {thread.id}: {exc}")


@bot.event
async def on_thread_create(thread: discord.Thread) -> None:
    # Auto-join newly created threads where allowed.
    try:
        if not thread.archived and thread.me is None:
            await thread.join()
            print(f"[thread-join] joined new thread {thread.id}")
    except discord.Forbidden:
        print(f"[thread-join] missing permission for new thread {thread.id}")
    except discord.HTTPException as exc:
        print(f"[thread-join] failed joining new thread {thread.id}: {exc}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if message.channel.type not in (
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.news_thread,
    ):
        return

    if isinstance(message.channel, discord.Thread):
        try:
            if not message.channel.archived and message.channel.me is None:
                await message.channel.join()
                print(f"[thread-join] joined thread on message {message.channel.id}")
        except discord.Forbidden:
            print(f"[thread-join] missing permission for thread {message.channel.id}")
        except discord.HTTPException as exc:
            print(f"[thread-join] failed joining thread {message.channel.id}: {exc}")

    with SessionLocal() as db:
        service.seed_default_gameplay_knowledge(db)
        thread_name = (getattr(message.channel, "name", "") or "").strip()
        campaign = service.get_or_create_campaign(
            db,
            thread_id=str(message.channel.id),
            mode="dnd",
            thread_name=thread_name,
        )
        if not service.reserve_discord_message_idempotency(
            db,
            campaign=campaign,
            discord_message_id=str(message.id),
            actor_discord_user_id=str(message.author.id),
        ):
            return
        player = service.ensure_player(db, campaign, str(message.author.id), message.author.display_name)
        actor_id = str(message.author.id)
        content = message.content.strip()
        lowered = content.lower()
        cmd_name = lowered.split()[0] if lowered.startswith("!") else ""

        if cmd_name == "!gmhelp":
            await _send_gm_help(message.channel)
            return

        if cmd_name == "!ping":
            started = _campaign_started(db, campaign)
            engine = service.turn_engine_for_campaign(db, campaign)
            await message.channel.send(
                f"Pong. mode={campaign.mode}, started={started}, engine={engine}, thread={message.channel.id}"
            )
            return

        if content.startswith("!adminauth "):
            token = content.removeprefix("!adminauth ").strip()
            ok = service.authenticate_sys_admin(db, actor_id, message.author.display_name, token)
            if ok:
                authenticated_admin_ids.add(actor_id)
                await message.channel.send("Sys admin authentication successful.")
            else:
                await message.channel.send("Sys admin authentication failed.")
            return

        if cmd_name == "!adminlogout":
            authenticated_admin_ids.discard(actor_id)
            await message.channel.send("Sys admin session cleared.")
            return

        is_admin = actor_id in authenticated_admin_ids and service.is_sys_admin(db, actor_id)

        if cmd_name == "!startstory":
            content = "!startgame story"
            cmd_name = "!startgame"

        if content.startswith("!startgame"):
            mode_arg = content.removeprefix("!startgame").strip().lower() or "dnd"
            if mode_arg not in {"dnd", "story"}:
                await message.channel.send("Usage: !startgame [dnd|story]")
                return
            generated = service.llm.generate_world_seed(mode=mode_arg)
            campaign.mode = mode_arg
            campaign.state = generated.model_dump()
            db.add(campaign)
            service.set_rule(db, campaign, "game_started", "true")
            if mode_arg == "story":
                service.set_rule(db, campaign, "agency_rule_profile", "minimal")
            scene_intro = str((campaign.state.get("flags", {}) or {}).get("scene_intro", "")).strip()
            opener = scene_intro or str(campaign.state.get("scene", "The story begins."))
            await message.channel.send(f"Game started in **{mode_arg}** mode.\n\n{opener}"[:1900])
            return

        allow_prestart_commands = {
            "!gmhelp",
            "!startgame",
            "!startstory",
            "!importcampaign",
            "!adminrestorecampaign",
            "!adminauth",
            "!adminlogout",
            "!adminrules",
            "!reportissue",
        }
        if not _campaign_started(db, campaign):
            if _is_admin_command(cmd_name, content):
                pass
            elif cmd_name in allow_prestart_commands:
                pass
            elif cmd_name.startswith("!"):
                await message.channel.send(
                    "This thread has no active game yet. Use `!startgame` or `!startstory` first."
                )
                return
            else:
                # Ignore natural-language game turns until a game is explicitly started.
                return

        if cmd_name == "!retry":
            ok, narration, details = service.retry_last_input_for_player(
                db,
                campaign,
                actor=actor_id,
                actor_display_name=message.author.display_name,
            )
            if not ok:
                await message.channel.send(narration)
            else:
                extra = f"\n\nRejected commands: {len(details.get('rejected', []))}" if details.get("rejected") else ""
                await message.channel.send(f"{narration}{extra}")
            return

        if content.startswith("!exportcampaign"):
            scope = content.removeprefix("!exportcampaign").strip() or "all"
            ok, key, detail = service.export_campaign_snapshot(
                db,
                campaign,
                actor_discord_user_id=actor_id,
                scope=scope,
            )
            if ok:
                await message.channel.send(f"Snapshot exported: `{key}` (scope={scope})")
            else:
                await message.channel.send(f"Export failed: {detail}")
            return

        if content.startswith("!importcampaign "):
            snapshot_key = content.removeprefix("!importcampaign ").strip()
            if not snapshot_key:
                await message.channel.send("Usage: !importcampaign <snapshot_key>")
                return
            ok, detail = service.import_campaign_snapshot_to_thread(
                db,
                target_thread_id=str(message.channel.id),
                actor_discord_user_id=actor_id,
                snapshot_key=snapshot_key,
                admin_force=False,
            )
            if ok:
                service.set_rule(db, campaign, "game_started", "true")
            await message.channel.send(detail if ok else f"Import failed: {detail}")
            return

        if content.startswith("!adminrestorecampaign "):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            snapshot_key = content.removeprefix("!adminrestorecampaign ").strip()
            if not snapshot_key:
                await message.channel.send("Usage: !adminrestorecampaign <snapshot_key>")
                return
            ok, detail = service.import_campaign_snapshot_to_thread(
                db,
                target_thread_id=str(message.channel.id),
                actor_discord_user_id=actor_id,
                snapshot_key=snapshot_key,
                admin_force=True,
            )
            if ok:
                service.set_rule(db, campaign, "game_started", "true")
            await message.channel.send(detail if ok else f"Restore failed: {detail}")
            return

        if content.startswith("!adminrules"):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            rows = service.admin_list_rule_blocks(db)
            rendered = "\n".join(f"- {r.rule_id} | {r.priority} | enabled={r.is_enabled}" for r in rows)
            await message.channel.send(f"System agency rules:\n{rendered[:1800]}")
            return

        if content.startswith("!adminrule add "):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            payload = content.removeprefix("!adminrule add ").strip()
            parts = [p.strip() for p in payload.split("|", 3)]
            if len(parts) != 4:
                await message.channel.send("Usage: !adminrule add <rule_id>|<title>|<priority>|<body>")
                return
            rule_id, title, priority, body = parts
            service.admin_upsert_rule_block(db, rule_id=rule_id, title=title, priority=priority, body=body)
            await message.channel.send(f"Admin rule upserted: {rule_id}")
            return

        if content.startswith("!adminrule update "):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            payload = content.removeprefix("!adminrule update ").strip()
            parts = [p.strip() for p in payload.split("|", 3)]
            if len(parts) != 4:
                await message.channel.send("Usage: !adminrule update <rule_id>|<title>|<priority>|<body>")
                return
            rule_id, title, priority, body = parts
            service.admin_upsert_rule_block(db, rule_id=rule_id, title=title, priority=priority, body=body)
            await message.channel.send(f"Admin rule updated: {rule_id}")
            return

        if content.startswith("!adminrule remove "):
            if not is_admin:
                await message.channel.send("Admin auth required. Use !adminauth <token>")
                return
            rule_id = content.removeprefix("!adminrule remove ").strip()
            ok = service.admin_remove_rule_block(db, rule_id)
            await message.channel.send("Rule removed." if ok else "Rule not found.")
            return

        if content.startswith("!setrule "):
            payload = content.removeprefix("!setrule ").strip()
            if "=" not in payload:
                await message.channel.send("Usage: !setrule key=value")
                return
            key, value = payload.split("=", 1)
            service.set_rule(db, campaign, key.strip(), value.strip())
            await message.channel.send(f"Rule '{key.strip()}' updated.")
            return

        if cmd_name == "!agencyprofiles":
            profiles = ", ".join(service.available_rule_profiles())
            await message.channel.send(f"Agency rule profiles: {profiles}")
            return

        if content.startswith("!setagencyprofile "):
            profile = content.removeprefix("!setagencyprofile ").strip().lower()
            if profile not in service.available_rule_profiles():
                await message.channel.send("Unknown profile. Use !agencyprofiles")
                return
            service.set_rule(db, campaign, "agency_rule_profile", profile)
            service.set_rule(db, campaign, "agency_rule_ids", "")
            await message.channel.send(f"Agency profile set to '{profile}'.")
            return

        if cmd_name == "!agencyrules":
            rules = "\n".join(f"- {rid}" for rid in service.available_rule_ids(db))
            await message.channel.send(f"Available agency rule IDs:\n{rules}")
            return

        if content.startswith("!setagencyrules "):
            csv_ids = content.removeprefix("!setagencyrules ").strip()
            selected = [x.strip() for x in csv_ids.split(",") if x.strip()]
            invalid = [x for x in selected if x not in service.available_rule_ids(db)]
            if invalid:
                await message.channel.send(f"Invalid rule IDs: {', '.join(invalid)}")
                return
            service.set_rule(db, campaign, "agency_rule_ids", ",".join(selected))
            await message.channel.send("Custom agency rule selection saved.")
            return

        if content.startswith("!setcharacter "):
            value = content.removeprefix("!setcharacter ").strip()
            if not value:
                await message.channel.send("Usage: !setcharacter <character instructions>")
                return
            service.set_rule(db, campaign, "character_instructions", value)
            await message.channel.send("Character instructions updated for this campaign.")
            return

        if content.startswith("!setprompt "):
            value = content.removeprefix("!setprompt ").strip()
            if not value:
                await message.channel.send("Usage: !setprompt <custom campaign directives>")
                return
            service.set_rule(db, campaign, "system_prompt_custom", value)
            await message.channel.send("Custom system prompt directives updated.")
            return

        if cmd_name == "!showprompt":
            prompt_text = service.build_campaign_system_prompt(db, campaign)
            if len(prompt_text) > 1800:
                prompt_text = prompt_text[:1800] + "\n...[truncated]"
            await message.channel.send(f"```\n{prompt_text}\n```")
            return

        if cmd_name == "!rules":
            custom = service.list_rules(db, campaign)
            effective = merge_rules(custom, mode=campaign.mode)
            rendered = "\n".join(f"- {k}: {v}" for k, v in effective.items())
            await message.channel.send(f"Effective rules:\n{rendered}")
            return

        if cmd_name == "!showruleset":
            active = service.get_campaign_ruleset(db, campaign)
            if not active:
                await message.channel.send("No active ruleset configured.")
                return
            await message.channel.send(
                f"Active ruleset: **{active.key}** ({active.name})\nSystem: {active.system} {active.version}\n{active.summary}"
            )
            return

        if content.startswith("!setruleset "):
            key = content.removeprefix("!setruleset ").strip()
            ok, detail = service.set_campaign_ruleset(db, campaign, key)
            await message.channel.send(f"Ruleset set to **{detail}**." if ok else f"Failed: {detail}")
            return

        if content.startswith("!rulelookup "):
            query = content.removeprefix("!rulelookup ").strip()
            if not query:
                await message.channel.send("Usage: !rulelookup <query>")
                return
            rows = service.rule_lookup_for_campaign(db, campaign, query, limit=3)
            if not rows:
                await message.channel.send("No matching rulebook entries found.")
                return
            lines: list[str] = []
            for r in rows:
                content_text = str(r.get("content", "")).strip()
                if len(content_text) > 240:
                    content_text = content_text[:237].rstrip() + "..."
                lines.append(
                    f"- **{r.get('title', '')}** ({r.get('rulebook', '')} {r.get('page_ref', '')})\n"
                    f"  {content_text}"
                )
            await message.channel.send("\n".join(lines)[:1900])
            return

        if content.startswith("!roll "):
            expr = content.removeprefix("!roll ").strip()
            ok, roll = service.roll_dice(expr)
            if not ok:
                await message.channel.send(str(roll.get("error", "Invalid roll.")))
                return
            service.log_dice_roll(
                db,
                campaign=campaign,
                actor_discord_user_id=actor_id,
                actor_display_name=message.author.display_name,
                roll_data=roll,
            )
            adv_mode = str(roll.get("advantage_mode", "none"))
            if adv_mode != "none":
                msg = (
                    f"`{roll['normalized_expression']}` -> rolls {roll['rolls']} "
                    f"({adv_mode}), picked **{roll['picked']}**, total **{roll['total']}**"
                )
            else:
                msg = (
                    f"`{roll['normalized_expression']}` -> rolls {roll['rolls']} "
                    f"+ {roll['modifier']} = **{roll['total']}**"
                )
            await message.channel.send(msg)
            return

        if cmd_name == "!mycharacter":
            description = content[len("!mycharacter") :].strip()
            if not description:
                await message.channel.send("Usage: !mycharacter <natural language character description>")
                return
            char_state = service.register_character_from_description(db, campaign, player, description)
            inventory_bits = [
                f"{qty} {item.replace('_', ' ')}" for item, qty in sorted(char_state.inventory.items()) if int(qty) > 0
            ]
            inventory_text = ", ".join(inventory_bits) if inventory_bits else "none"
            await message.channel.send(
                (
                    f"Character linked to you: **{char_state.name}**\n"
                    f"Description: {char_state.description or '(none)'}\n"
                    f"Starting equipment: {inventory_text}"
                )[:1900]
            )
            return

        if cmd_name == "!deletecharacter":
            ok, detail = service.delete_player_character(db, campaign, player)
            await message.channel.send(detail if ok else f"Failed: {detail}")
            return

        if cmd_name == "!agentmode":
            mode = content.removeprefix("!agentmode").strip().lower()
            if not mode:
                await message.channel.send(f"Current engine: **{service.turn_engine_for_campaign(db, campaign)}**")
                return
            if mode not in {"classic", "crew"}:
                await message.channel.send("Usage: !agentmode [classic|crew]")
                return
            service.set_rule(db, campaign, "turn_engine", mode)
            await message.channel.send(f"Turn engine set to **{mode}**.")
            return

        if content.startswith("!teach "):
            payload = content.removeprefix("!teach ").strip()
            parts = [p.strip() for p in payload.split("|", 2)]
            if len(parts) != 3:
                await message.channel.send("Usage: !teach <item|effect>|<key>|<observation>")
                return
            ok, detail = service.teach_knowledge(
                db,
                campaign,
                actor_id,
                kind=parts[0],
                key=parts[1],
                observation=parts[2],
            )
            await message.channel.send(detail if ok else f"Failed: {detail}")
            return

        if content.startswith("!reportissue"):
            issue_text = content[len("!reportissue") :].strip()
            if not issue_text:
                await message.channel.send("Usage: !reportissue <what went wrong>")
                return
            ok, detail = service.report_player_issue(
                db,
                campaign=campaign,
                actor_discord_user_id=actor_id,
                actor_display_name=message.author.display_name,
                message=issue_text,
            )
            await message.channel.send(detail if ok else f"Failed: {detail}")
            return

        if content.startswith("!additem "):
            parts = content.split()
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
                user_input=content,
                commands=[Command(type="add_item", target=character_name, key=item_key, amount=quantity)],
            )
            await message.channel.send("Item added." if ok else f"Failed to add item: {detail}")
            return

        if content.startswith("!addeffect "):
            payload = content.removeprefix("!addeffect ").strip()
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
                user_input=content,
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

        if cmd_name.startswith("!"):
            guess = service.llm.infer_discord_command(content, _possible_commands())
            candidate = str(guess.get("matched_command", "") or "").strip()
            if candidate:
                await message.channel.send(
                    f"Unknown command `{cmd_name}`. Closest valid command: `{candidate}`. Use `!gmhelp` for the full list."
                )
            else:
                await message.channel.send(f"Unknown command `{cmd_name}`. Use `!gmhelp`.")
            return

        global turn_job_queue
        if turn_job_queue is None:
            turn_job_queue = asyncio.Queue(maxsize=max(1, int(settings.turn_worker_queue_max)))
        job = TurnJob(
            channel=message.channel,
            campaign_id=int(campaign.id),
            actor_id=actor_id,
            actor_display_name=message.author.display_name,
            user_input=content,
        )
        try:
            turn_job_queue.put_nowait(job)
        except asyncio.QueueFull:
            await message.channel.send("Turn queue is currently full. Please retry in a few moments.")
            return


if __name__ == "__main__":
    init_db()
    if not settings.discord_token:
        raise RuntimeError("Set AIGM_DISCORD_TOKEN in environment")
    bot.run(settings.discord_token)
