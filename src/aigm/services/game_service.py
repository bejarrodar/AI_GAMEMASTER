from __future__ import annotations

import json
import hashlib
import hmac
import re
import secrets
import traceback
import uuid
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, ValidationError

from aigm.adapters.llm import LLMAdapter
from aigm.agents.crew import CrewOrchestrator
from aigm.config import settings
from aigm.core.context_builder import ContextBuilder
from aigm.core.prompts import DEFAULT_RULE_BLOCKS, RULE_PROFILES, build_system_prompt, rule_ids_for_profile
from aigm.core.state_machine import apply_commands, tick_effects
from aigm.core.validator import validate_commands
from aigm.db.models import (
    AdminAuditLog,
    AgencyRuleBlock,
    AuthPermission,
    AuthRole,
    AuthRolePermission,
    AuthUser,
    AuthUserRole,
    Campaign,
    CampaignMemorySummary,
    CampaignRule,
    CampaignSnapshot,
    Character,
    EffectKnowledge,
    EffectObservation,
    DiceRollLog,
    GameRuleset,
    GlobalEffectRelevance,
    GlobalLearnedRelevance,
    InventoryItem,
    ItemKnowledge,
    ItemObservation,
    Player,
    ProcessedDiscordMessage,
    Rulebook,
    RulebookEntry,
    SysAdminUser,
    TurnLog,
)
from aigm.ops.db_api_client import DBApiClient
from aigm.schemas.game import CharacterState, Command, PlayerIntentExtraction, WorldState


class AIRawOutputEnvelope(BaseModel):
    schema_version: int = 2
    source: str


class GameService:
    AI_RAW_OUTPUT_SCHEMA_VERSION = 2
    DEFAULT_PERMISSIONS: dict[str, str] = {
        "campaign.read": "View campaigns and state.",
        "campaign.play": "Submit player turns.",
        "campaign.retry": "Retry last player turn.",
        "campaign.write": "Modify campaign rules/config.",
        "campaign.import": "Import campaign snapshot.",
        "campaign.export": "Export campaign snapshot.",
        "rules.manage": "Manage agency/system rule blocks.",
        "system.admin": "System administration operations.",
        "user.manage": "Manage auth users and roles.",
    }
    DEFAULT_ROLES: dict[str, dict] = {
        "viewer": {
            "description": "Read-only access.",
            "permissions": {"campaign.read"},
        },
        "player": {
            "description": "Player turn access.",
            "permissions": {"campaign.read", "campaign.play", "campaign.retry", "campaign.export", "campaign.import"},
        },
        "gm": {
            "description": "Game master controls.",
            "permissions": {
                "campaign.read",
                "campaign.play",
                "campaign.retry",
                "campaign.write",
                "campaign.export",
                "campaign.import",
                "rules.manage",
            },
        },
        "admin": {
            "description": "Full admin access.",
            "permissions": set(DEFAULT_PERMISSIONS.keys()),
        },
    }
    RELEVANCE_TAG_HINTS = {
        "night": {"night", "midnight", "moon", "moonlight", "dusk"},
        "day": {"day", "sun", "sunlight", "noon", "morning", "dawn"},
        "dark": {"dark", "shadow", "dim", "cave"},
        "town": {"town", "city", "village", "square", "market", "street"},
        "forest": {"forest", "woods", "grove", "jungle"},
        "ruins": {"ruins", "temple", "ancient"},
        "combat": {"fight", "combat", "battle", "attack", "enemy"},
        "shop": {"shop", "store", "merchant", "vendor"},
    }
    ITEM_ACTION_VERBS = {
        "hit",
        "attack",
        "swing",
        "stab",
        "slash",
        "shoot",
        "throw",
        "use",
        "cast",
        "fire",
        "smash",
        "strike",
    }
    NON_ITEM_MY_WORDS = {
        "name",
        "friend",
        "party",
        "team",
        "group",
        "character",
        "turn",
        "hp",
        "life",
        "hand",
        "hands",
        "arm",
        "head",
        "body",
    }
    PLAYER_ACTION_VERBS = {
        "looks",
        "look",
        "checks",
        "check",
        "begins",
        "begin",
        "searches",
        "search",
        "scans",
        "scan",
        "dodges",
        "dodge",
        "moves",
        "move",
        "says",
        "say",
        "attacks",
        "attack",
        "swings",
        "swing",
        "opens",
        "open",
        "runs",
        "run",
        "walks",
        "walk",
    }
    MONEY_ITEM_KEYS = {"gold", "gold_coin", "gold_coins", "coin", "coins", "silver", "silver_coin", "silver_coins"}
    PICKUP_PATTERNS = [
        r"\bpick\s+up\s+(?:a|an|the|my)?\s*([a-z][a-z0-9_\-']*)\b",
        r"\bpickup\s+(?:a|an|the|my)?\s*([a-z][a-z0-9_\-']*)\b",
        r"\bgrab\s+(?:a|an|the|my)?\s*([a-z][a-z0-9_\-']*)\b",
        r"\btake\s+(?:a|an|the|my)?\s*([a-z][a-z0-9_\-']*)\b",
    ]
    INVENTORY_PULL_PATTERNS = [
        r"\b(?:pull|draw|take|get|grab|use|ready|equip)\s+(?:a|an|the|my)?\s*([a-z][a-z0-9_\-']*)\s+"
        r"(?:out\s+of|from)\s+my\s+inventory\b",
    ]
    INVENTORY_ADD_PATTERNS = [
        r"\b(?:put|place|stash|store|add)\s+(.+?)\s+into\s+my\s+inventory\b",
    ]
    STEAL_PATTERNS = [
        r"\bsteal\s+(?:a|an|the)?\s*([a-z][a-z0-9_\-']*)\s+from\s+([a-z][a-z0-9_\-']*)\b",
        r"\btake\s+(?:a|an|the)?\s*([a-z][a-z0-9_\-']*)\s+from\s+([a-z][a-z0-9_\-']*)\b",
        r"\bgrab\s+(?:a|an|the)?\s*([a-z][a-z0-9_\-']*)\s+from\s+([a-z][a-z0-9_\-']*)\b",
    ]
    INPUT_ALIGNMENT_STOPWORDS = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "them",
        "then",
        "to",
        "up",
        "with",
        "you",
        "your",
    }
    LOW_INFO_NARRATION_MARKERS = {
        "the scene shifts",
        "the moment hangs",
        "what do you do next",
        "in tense silence",
    }
    ATTACK_FAIL_MARKERS = {
        "miss",
        "misses",
        "missed",
        "dodge",
        "dodges",
        "dodged",
        "blocked",
        "glances off",
        "hits the wall",
        "hit the wall",
        "whiffs",
        "fails to hit",
        "doesn't hit",
        "does not hit",
    }
    HARMFUL_EFFECT_HINTS = {"poison", "bleed", "burn", "curse", "hex", "wound", "fracture", "stun", "frostbite"}
    DICE_EXPR_RE = re.compile(
        r"^\s*(?:(adv|advantage|dis|disadvantage)\s+)?(\d{0,3})d(\d{1,4})\s*([+-]\s*\d{1,4})?\s*$",
        re.IGNORECASE,
    )
    DEFAULT_GAME_RULESETS = (
        {
            "key": "dnd5e-2014",
            "name": "D&D 5e (2014)",
            "system": "dnd",
            "version": "5e-2014",
            "summary": "Classic 5e baseline with advantage/disadvantage and proficiency-driven checks.",
            "is_official": True,
            "rules_json": {"hit_die_baseline": "d20", "proficiency_system": True, "short_rest": True},
        },
        {
            "key": "dnd5e-2024",
            "name": "D&D 5e (2024)",
            "system": "dnd",
            "version": "5e-2024",
            "summary": "Revised 5e ruleset with updated class and encounter assumptions.",
            "is_official": True,
            "rules_json": {"hit_die_baseline": "d20", "proficiency_system": True, "revised_rules": True},
        },
        {
            "key": "story-freeform",
            "name": "Story Freeform",
            "system": "story",
            "version": "v1",
            "summary": "Narrative-first mode with minimal mechanical constraints.",
            "is_official": False,
            "rules_json": {"dice_optional": True, "strict_combat": False},
        },
    )
    DEFAULT_RULEBOOKS = (
        {
            "slug": "dnd5e-srd-basics",
            "title": "D&D 5e SRD Basics",
            "system": "dnd",
            "version": "5e",
            "source": "SRD-derived notes",
            "summary": "Core mechanical references for checks, combat, and conditions.",
            "entries": (
                {
                    "entry_key": "ability_checks",
                    "title": "Ability Checks",
                    "section": "Core Mechanics",
                    "page_ref": "SRD Ch. 7",
                    "tags": ["ability", "check", "dc", "proficiency"],
                    "content": (
                        "Roll a d20 and add the relevant ability modifier. "
                        "Add proficiency bonus if proficient. Compare total against DC."
                    ),
                },
                {
                    "entry_key": "advantage_disadvantage",
                    "title": "Advantage and Disadvantage",
                    "section": "Core Mechanics",
                    "page_ref": "SRD Ch. 7",
                    "tags": ["advantage", "disadvantage", "d20"],
                    "content": "Roll two d20s. Use the higher for advantage, lower for disadvantage.",
                },
                {
                    "entry_key": "death_saves",
                    "title": "Death Saving Throws",
                    "section": "Combat",
                    "page_ref": "SRD Ch. 9",
                    "tags": ["death", "saving throw", "combat"],
                    "content": (
                        "At 0 HP and not stable: roll d20 each turn. "
                        "10+ is success, 9- is failure. Three successes stabilize; three failures die."
                    ),
                },
            ),
        },
    )

    def __init__(self, llm: LLMAdapter):
        self.llm = llm
        self.context_builder = ContextBuilder()
        self.crew = CrewOrchestrator(llm)
        self.db_api = DBApiClient(settings.db_api_url, token=settings.db_api_token)

    @staticmethod
    def _new_turn_correlation_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _tokenize_for_search(text: str) -> list[str]:
        return [t for t in re.findall(r"[a-z0-9_']+", (text or "").lower()) if len(t) >= 2]

    @staticmethod
    def _normalize_ruleset_key(raw: str) -> str:
        return re.sub(r"[^a-z0-9_\-]+", "-", (raw or "").strip().lower()).strip("-")

    def seed_default_gameplay_knowledge(self, db: Session) -> None:
        changed = False
        for row in self.DEFAULT_GAME_RULESETS:
            key = self._normalize_ruleset_key(str(row["key"]))
            existing = db.query(GameRuleset).filter(GameRuleset.key == key).one_or_none()
            if existing:
                continue
            db.add(
                GameRuleset(
                    key=key,
                    name=str(row["name"]),
                    system=str(row["system"]),
                    version=str(row["version"]),
                    summary=str(row["summary"]),
                    is_official=bool(row["is_official"]),
                    is_enabled=True,
                    rules_json=dict(row.get("rules_json", {}) or {}),
                )
            )
            changed = True
        if changed:
            db.flush()

        for book in self.DEFAULT_RULEBOOKS:
            slug = str(book["slug"]).strip().lower()
            rulebook = db.query(Rulebook).filter(Rulebook.slug == slug).one_or_none()
            if not rulebook:
                rulebook = Rulebook(
                    slug=slug,
                    title=str(book["title"]),
                    system=str(book["system"]),
                    version=str(book["version"]),
                    source=str(book.get("source", "")),
                    summary=str(book.get("summary", "")),
                    is_enabled=True,
                )
                db.add(rulebook)
                changed = True
                db.flush()
            existing_keys = {
                r.entry_key
                for r in db.query(RulebookEntry).filter(RulebookEntry.rulebook_id == rulebook.id).all()
            }
            for entry in list(book.get("entries", ())):
                entry_key = str(entry["entry_key"]).strip().lower()
                if entry_key in existing_keys:
                    continue
                content = str(entry.get("content", "")).strip()
                tags = [str(t).strip().lower() for t in list(entry.get("tags", []) or []) if str(t).strip()]
                search_blob = " ".join(
                    [str(entry.get("title", "")), str(entry.get("section", "")), content, " ".join(tags)]
                ).lower()
                db.add(
                    RulebookEntry(
                        rulebook_id=rulebook.id,
                        entry_key=entry_key,
                        title=str(entry.get("title", entry_key)),
                        section=str(entry.get("section", "")),
                        page_ref=str(entry.get("page_ref", "")),
                        tags=tags,
                        content=content,
                        searchable_text=search_blob,
                    )
                )
                changed = True
        if changed:
            db.commit()

    @classmethod
    def _mentioned_personal_items(cls, user_input: str) -> set[str]:
        lower = user_input.lower()
        if not any(re.search(rf"\b{verb}\b", lower) for verb in cls.ITEM_ACTION_VERBS):
            return set()
        found = re.findall(r"\bmy\s+([a-z][a-z0-9_\-']*)\b", lower, flags=re.IGNORECASE)
        items = {x.strip().lower() for x in found}
        return {x for x in items if x and x not in cls.NON_ITEM_MY_WORDS}

    @classmethod
    def _first_missing_personal_item(cls, user_input: str, inventory_keys: set[str]) -> str | None:
        mentioned = cls._mentioned_personal_items(user_input) | cls._inventory_pull_items(user_input)
        if not mentioned:
            return None
        inv = {k.lower() for k in inventory_keys}
        for item in sorted(mentioned):
            if item not in inv:
                return item
        return None

    @staticmethod
    def _normalize_item_key(raw: str) -> str:
        text = raw.strip().lower()
        text = re.sub(r"^(?:my|the|a|an)\s+", "", text)
        text = re.sub(r"[^a-z0-9\s_\-']", "", text)
        text = re.sub(r"\s+", "_", text).strip("_")
        return text

    @classmethod
    def _inventory_pull_items(cls, user_input: str) -> set[str]:
        lower = user_input.lower()
        found: set[str] = set()
        for pattern in cls.INVENTORY_PULL_PATTERNS:
            for match in re.finditer(pattern, lower, flags=re.IGNORECASE):
                key = cls._normalize_item_key(match.group(1))
                if key and key not in cls.NON_ITEM_MY_WORDS:
                    found.add(key)
        return found

    @classmethod
    def _inventory_add_items(cls, user_input: str) -> list[tuple[str, int]]:
        lower = user_input.lower()
        for pattern in cls.INVENTORY_ADD_PATTERNS:
            match = re.search(pattern, lower, flags=re.IGNORECASE)
            if not match:
                continue
            payload = match.group(1).strip()
            parts = re.split(r"\s+and\s+|,\s*", payload)
            parsed: list[tuple[str, int]] = []
            for part in parts:
                p = part.strip()
                if not p:
                    continue
                qty = 1
                qty_match = re.match(r"^(?:the\s+)?(\d+)\s+(.+)$", p)
                if qty_match:
                    qty = int(qty_match.group(1))
                    p = qty_match.group(2).strip()
                key = cls._normalize_item_key(p)
                if key:
                    parsed.append((key, max(1, qty)))
            return parsed
        return []

    def _missing_item_guard_message(
        self, db: Session, campaign: Campaign, player: Player, user_input: str, current_state: WorldState
    ) -> tuple[str | None, str | None]:
        char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        if not char_name:
            return None, None
        char = current_state.party.get(char_name)
        if not char:
            return None, None
        missing_item = self._first_missing_personal_item(user_input, set(char.inventory.keys()))
        if not missing_item:
            return None, None
        narration = (
            f"You can't use '{missing_item}' because it is not in your inventory. "
            "Choose another action or acquire the item first."
        )
        return missing_item, narration

    @classmethod
    def _pickup_item_mentioned(cls, user_input: str) -> str | None:
        lower = user_input.lower()
        for pattern in cls.PICKUP_PATTERNS:
            match = re.search(pattern, lower, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().lower()
        return None

    @staticmethod
    def _can_find_item_in_scene(scene: str, item_key: str) -> bool:
        scene_lower = scene.lower()
        if item_key == "stick":
            outdoor_hints = {
                "forest",
                "woods",
                "jungle",
                "grove",
                "field",
                "road",
                "trail",
                "frontier",
                "ruins",
                "town",
                "market",
                "camp",
                "outside",
                "dawn",
            }
            return any(hint in scene_lower for hint in outdoor_hints)
        return False

    def _resolve_pickup_action(
        self, db: Session, campaign: Campaign, player: Player, user_input: str, current_state: WorldState
    ) -> dict | None:
        item_key = self._pickup_item_mentioned(user_input)
        if not item_key:
            return None

        char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        if not char_name or char_name not in current_state.party:
            return None
        char = current_state.party[char_name]

        if char.inventory.get(item_key, 0) > 0:
            return {
                "handled": True,
                "found": False,
                "accepted": [],
                "rejected": [{"reason": f"item_already_owned:{item_key}"}],
                "narration": f"You already have a {item_key} in your inventory.",
            }

        if self._can_find_item_in_scene(current_state.scene, item_key):
            cmd = Command(type="add_item", target=char_name, key=item_key, amount=1)
            return {
                "handled": True,
                "found": True,
                "accepted": [cmd],
                "rejected": [],
                "narration": f"You search the area and find a {item_key}. It is now in your inventory.",
            }

        return {
            "handled": True,
            "found": False,
            "accepted": [],
            "rejected": [{"reason": f"item_not_found_in_scene:{item_key}"}],
            "narration": f"You search around for a {item_key} but don't find one in this area.",
        }

    def _resolve_inventory_add_action(
        self, db: Session, campaign: Campaign, player: Player, user_input: str, current_state: WorldState
    ) -> dict | None:
        requested = self._inventory_add_items(user_input)
        if not requested:
            return None
        char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        if not char_name or char_name not in current_state.party:
            return None

        commands: list[Command] = []
        for key, qty in requested:
            commands.append(Command(type="add_item", target=char_name, key=key, amount=qty))

        pretty_items = ", ".join(f"{qty} {key.replace('_', ' ')}" for key, qty in requested)
        return {
            "handled": True,
            "accepted": commands,
            "rejected": [],
            "narration": f"You place {pretty_items} into your inventory.",
        }

    @classmethod
    def _steal_item_request(cls, user_input: str) -> tuple[str, str] | None:
        lower = user_input.lower()
        for pattern in cls.STEAL_PATTERNS:
            match = re.search(pattern, lower, flags=re.IGNORECASE)
            if match:
                item = match.group(1).strip().lower()
                target = match.group(2).strip()
                if item and target:
                    return item, target
        return None

    @staticmethod
    def _resolve_party_name_ci(state: WorldState, requested_name: str) -> str | None:
        req = requested_name.strip().lower()
        for existing in state.party.keys():
            if existing.lower() == req:
                return existing
        return None

    def _steal_guard_message(self, user_input: str, current_state: WorldState) -> tuple[str | None, str | None]:
        parsed = self._steal_item_request(user_input)
        if not parsed:
            return None, None
        item_key, target_name_raw = parsed
        target_name = self._resolve_party_name_ci(current_state, target_name_raw)
        if not target_name:
            return None, None
        target = current_state.party[target_name]
        if target.inventory.get(item_key, 0) > 0:
            return None, None
        narration = (
            f"{target_name} does not have a '{item_key}' in their inventory, "
            f"so you cannot steal it right now."
        )
        return item_key, narration

    @staticmethod
    def _augment_system_prompt_with_runtime_constraints(system_prompt: str, constraints: list[str]) -> str:
        if not constraints:
            return system_prompt
        rendered = "\n".join(f"- {c}" for c in constraints)
        return (
            f"{system_prompt}\n\n"
            "RUNTIME STATE CONSTRAINTS (MUST OBEY):\n"
            f"{rendered}\n"
            "If an attempted action depends on an unavailable item, narrate the attempt and failure grounded in "
            "world state. Do not fabricate successful possession, transfer, or use of missing items."
        )

    @staticmethod
    def _is_purchase_attempt(user_input: str) -> bool:
        return bool(re.search(r"\b(buy|purchase|pay|paid|spend)\b", (user_input or "").lower()))

    @classmethod
    def _has_currency(cls, inventory: dict[str, int]) -> bool:
        return any((k.lower() in cls.MONEY_ITEM_KEYS and v > 0) for k, v in (inventory or {}).items())

    @staticmethod
    def _normalize_currency_item_key(currency: str) -> str:
        raw = (currency or "").strip().lower().replace(" ", "_")
        aliases = {
            "gold": "gold_coins",
            "coin": "coins",
            "coins": "coins",
            "gold_coin": "gold_coins",
            "gold_coins": "gold_coins",
            "silver": "silver_coins",
            "silver_coin": "silver_coins",
            "silver_coins": "silver_coins",
        }
        return aliases.get(raw, raw)

    @classmethod
    def _currency_amount(cls, inventory: dict[str, int], currency: str) -> int:
        key = cls._normalize_currency_item_key(currency)
        if not key:
            return 0
        return int((inventory or {}).get(key, 0))

    @staticmethod
    def _is_shop_context(scene: str, user_input: str) -> bool:
        text = f"{scene} {user_input}".lower()
        hints = ("shop", "merchant", "vendor", "store", "market", "counter", "guild trader")
        return any(h in text for h in hints)

    @staticmethod
    def _actor_inventory_for_context(db: Session, campaign: Campaign, actor_discord_user_id: str, state: WorldState) -> dict[str, int]:
        player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor_discord_user_id)
            .one_or_none()
        )
        if not player:
            return {}
        row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign.id, Character.player_id == player.id)
            .one_or_none()
        )
        if not row:
            return {}
        char = state.party.get(row.name)
        if not char:
            return {}
        return char.inventory

    @staticmethod
    def _narration_already_failure_style(text: str) -> bool:
        lower = text.lower()
        failure_markers = ("can't", "cannot", "fail", "fails", "failed", "unable", "does not", "doesn't", "not possible")
        return any(m in lower for m in failure_markers)

    @classmethod
    def _enforce_infeasible_intent_on_narration(cls, narration: str, intent: PlayerIntentExtraction) -> str:
        infeasible = [c for c in intent.feasibility_checks if not c.is_possible]
        if not infeasible:
            return narration
        if cls._narration_already_failure_style(narration):
            return narration
        first = infeasible[0]
        reason = first.reason.strip() or "That action is not possible in the current state."
        return f"You attempt the action, but it fails: {reason}"

    @staticmethod
    def _requires_explicit_feasibility_resolution(check) -> bool:
        reason = (check.reason or "").strip().lower()
        return "requires explicit feasibility assessment" in reason

    @staticmethod
    def _dedupe_rejections(rows: list[dict]) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for row in rows:
            key = json.dumps(row, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    @staticmethod
    def _strip_system_prompt_leakage(narration: str) -> str:
        text = (narration or "").strip()
        if not text:
            return text

        # Remove direct quoted leaks like: The Game Master says: "PLAYER AGENCY RULESET ..."
        text = re.sub(
            r'\bthe\s+game\s+master\s+says:\s*".*?"',
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        leak_markers = (
            "player agency ruleset",
            "follow each rule block exactly",
            "if rules conflict",
            "priority wins",
            "choose the safer action that preserves agency",
            "system prompt",
            "format:",
            "priority:",
        )
        kept: list[str] = []
        for segment in re.split(r"(?<=[.!?])\s+", text):
            s = segment.strip()
            if not s:
                continue
            lower = s.lower()
            if any(marker in lower for marker in leak_markers):
                continue
            kept.append(s)

        cleaned = " ".join(kept).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or "You attempt the action, but the world resists in an unclear way."

    @staticmethod
    def _normalize_quoted_player_input(user_input: str, speaker_name: str | None) -> str:
        text = (user_input or "").strip()
        if not text:
            return text
        # If the whole input is quoted speech, convert it to explicit dialogue attribution.
        quote_match = re.match(r'^\s*["â€œ](.+?)["â€]\s*$', text, flags=re.DOTALL)
        if not quote_match:
            return text
        spoken = quote_match.group(1).strip()
        if not spoken:
            return text
        speaker = (speaker_name or "Player").strip() or "Player"
        return f'{speaker} says: "{spoken}"'

    @staticmethod
    def _format_inventory_list(inventory: dict[str, int]) -> str:
        rows = [(k, int(v)) for k, v in (inventory or {}).items() if int(v) > 0]
        if not rows:
            return ""
        rows.sort(key=lambda item: item[0])
        return ", ".join(f"{qty} {key.replace('_', ' ')}" for key, qty in rows)

    def _self_inspection_narration(
        self, state: WorldState, actor_character_name: str | None, user_input: str
    ) -> str | None:
        if not actor_character_name:
            return None
        char = state.party.get(actor_character_name)
        if not char:
            return None
        text = (user_input or "").strip().lower()
        if not text:
            return None

        asks_appearance = any(
            phrase in text
            for phrase in (
                "what do i look like",
                "how do i look",
                "describe me",
                "my appearance",
                "what am i wearing",
            )
        )
        if asks_appearance:
            if char.description.strip():
                return f"You are **{char.name}**. {char.description.strip()}"
            return f"You are **{char.name}**. Your appearance is still undefined."

        asks_equipment = any(
            phrase in text
            for phrase in (
                "what am i equipped with",
                "what am i equipped wi th",
                "what am i carrying",
                "what do i have equipped",
                "show my inventory",
                "what is in my inventory",
                "what do i have in my inventory",
                "what am i holding",
            )
        )
        if asks_equipment:
            items = self._format_inventory_list(char.inventory)
            if items:
                return f"You are currently carrying: {items}."
            return "You are not carrying any items right now."
        return None

    @staticmethod
    def _full_conversation_for_context(db: Session, campaign: Campaign) -> list[dict]:
        actor_name_by_id = {
            p.discord_user_id: p.display_name
            for p in db.query(Player).filter(Player.campaign_id == campaign.id).all()
        }
        turns = (
            db.query(TurnLog)
            .filter(TurnLog.campaign_id == campaign.id)
            .order_by(TurnLog.id.asc())
            .all()
        )
        history: list[dict] = []
        for t in turns:
            history.append(
                {
                    "turn_id": t.id,
                    "actor_id": t.actor,
                    "actor_name": actor_name_by_id.get(t.actor, t.actor),
                    "user_input": t.user_input,
                    "narration": t.narration,
                }
            )
        return history

    @classmethod
    def _tokenize_for_alignment(cls, text: str) -> set[str]:
        return {
            t
            for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(t) >= 3 and t not in cls.INPUT_ALIGNMENT_STOPWORDS
        }

    @classmethod
    def _alignment_score(cls, user_input: str, narration: str) -> float:
        in_tokens = cls._tokenize_for_alignment(user_input)
        if not in_tokens:
            return 1.0
        out_tokens = cls._tokenize_for_alignment(narration)
        overlap = len(in_tokens & out_tokens)
        return overlap / max(1, len(in_tokens))

    @classmethod
    def _fails_input_probability_check(cls, user_input: str, narration: str) -> bool:
        lower = (narration or "").strip().lower()
        if not lower:
            return True
        if any(marker in lower for marker in cls.LOW_INFO_NARRATION_MARKERS):
            return True
        score = cls._alignment_score(user_input, narration)
        if len(cls._tokenize_for_alignment(user_input)) >= 5 and score < 0.15:
            return True
        return False

    @classmethod
    def _apply_local_probability_check(cls, review: dict, user_input: str, narration: str) -> dict:
        out = dict(review)
        score = cls._alignment_score(user_input, narration)
        out["alignment_score"] = max(0.0, min(1.0, score))
        input_aligned = not cls._fails_input_probability_check(user_input, narration)
        out["input_aligned"] = input_aligned
        violations = list(out.get("violations", []))
        if not input_aligned and "input_mismatch" not in violations:
            violations.append("input_mismatch")
        out["violations"] = violations
        if not input_aligned:
            out["plausible"] = False
        return out

    @staticmethod
    def _should_apply_reviewer_rewrite(review_payload: dict) -> bool:
        if not bool(review_payload.get("plausible", True)):
            return True
        if bool(review_payload.get("breaks_pc_autonomy", False)):
            return True
        if review_payload.get("violations"):
            return True
        if not bool(review_payload.get("input_aligned", True)):
            return True
        return False

    @classmethod
    def _narration_indicates_attack_failure(cls, narration: str) -> bool:
        lower = (narration or "").lower()
        return any(marker in lower for marker in cls.ATTACK_FAIL_MARKERS)

    @classmethod
    def _command_is_harmful(cls, cmd: Command) -> bool:
        if cmd.type == "adjust_hp":
            return isinstance(cmd.amount, int) and cmd.amount < 0
        if cmd.type == "add_effect":
            key = (cmd.key or "").lower()
            text = (cmd.text or "").lower()
            return any(h in key or h in text for h in cls.HARMFUL_EFFECT_HINTS)
        return False

    def _filter_commands_for_narrative_outcome(
        self,
        commands: list[Command],
        narration: str,
    ) -> tuple[list[Command], list[dict]]:
        if not self._narration_indicates_attack_failure(narration):
            return commands, []
        kept: list[Command] = []
        rejected: list[dict] = []
        for cmd in commands:
            if self._command_is_harmful(cmd):
                rejected.append(
                    {
                        "command": cmd.model_dump_json(),
                        "reason": "Rejected by outcome consistency guard: narration indicates attack failure.",
                    }
                )
                continue
            kept.append(cmd)
        return kept, rejected

    @staticmethod
    def _build_story_continuation_failure_narration(user_input: str, state: WorldState) -> str:
        attempt = (user_input or "You act").strip()
        attempt = re.sub(r"\s+", " ", attempt)
        scene = (state.scene or "").strip()
        scene_line = f"{scene} " if scene else ""
        return (
            f"{scene_line}You try it, but the world pushes back before your action can fully resolve: {attempt}. "
            "People nearby react with alarm, and the situation escalates around you. What do you do next?"
        )

    @staticmethod
    def _contains_non_english_script(text: str) -> bool:
        return bool(
            re.search(
                r"[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]",
                text or "",
            )
        )

    @staticmethod
    def _user_requested_non_english(user_input: str) -> bool:
        lower = (user_input or "").lower()
        return any(
            hint in lower
            for hint in (
                "in spanish",
                "in french",
                "in german",
                "in japanese",
                "in chinese",
                "in korean",
                "in russian",
                "en espaÃ±ol",
                "translate to",
            )
        )

    @classmethod
    def _narration_violates_other_player_agency(
        cls, narration: str, actor_character_name: str | None, player_character_names: list[str]
    ) -> str | None:
        text = narration.lower()
        actor_l = (actor_character_name or "").lower().strip()
        for name in player_character_names:
            name_l = name.lower().strip()
            if not name_l or name_l == actor_l:
                continue
            name_pat = re.escape(name_l)
            for verb in cls.PLAYER_ACTION_VERBS:
                if re.search(rf"\b{name_pat}\b[\s,]+{verb}\b", text):
                    return name
            # Stronger guard: another player name starting a sentence is usually puppeteering.
            if re.search(rf"(^|[.!?]\s+){name_pat}\b", text):
                return name
        return None

    def _enforce_other_player_agency_on_narration(
        self,
        db: Session,
        campaign: Campaign,
        actor_discord_user_id: str,
        narration: str,
    ) -> str:
        actor_player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor_discord_user_id)
            .one_or_none()
        )
        actor_char_name = None
        if actor_player:
            actor_char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=actor_player.id)

        all_player_chars = [
            row.name for row in db.query(Character).filter(Character.campaign_id == campaign.id).all() if row.player_id
        ]
        violating_name = self._narration_violates_other_player_agency(narration, actor_char_name, all_player_chars)
        if not violating_name:
            return narration

        def _normalize_second_person(text: str) -> str:
            out = text
            replacements = {
                r"\bYou begins\b": "You begin",
                r"\bYou searches\b": "You search",
                r"\bYou scans\b": "You scan",
                r"\bYou looks\b": "You look",
                r"\bYou checks\b": "You check",
                r"\bYou dodges\b": "You dodge",
                r"\bYou moves\b": "You move",
                r"\bYou says\b": "You say",
                r"\bYou attacks\b": "You attack",
                r"\bYou swings\b": "You swing",
                r"\bYou opens\b": "You open",
                r"\bYou runs\b": "You run",
                r"\bYou walks\b": "You walk",
            }
            for pattern, repl in replacements.items():
                out = re.sub(pattern, repl, out)
            out = re.sub(r"\bhis\b", "your", out, flags=re.IGNORECASE)
            out = re.sub(r"\bher\b", "your", out, flags=re.IGNORECASE)
            out = re.sub(r"\bhim\b", "you", out, flags=re.IGNORECASE)
            return out

        # If model accidentally narrates another player as the acting subject,
        # rewrite the line to second-person instead of dead-ending the turn.
        if actor_char_name:
            pattern = rf"^\s*{re.escape(violating_name)}\b"
            if re.search(pattern, narration, flags=re.IGNORECASE):
                rewritten = re.sub(pattern, "You", narration, count=1, flags=re.IGNORECASE)
                return _normalize_second_person(rewritten)

        return (
            f"You attempt the action toward {violating_name}, but their response is up to that player. "
            f"Waiting for {violating_name}'s declared action."
        )

    @staticmethod
    def _augment_system_prompt_with_actor(system_prompt: str, actor_character_name: str | None) -> str:
        if not actor_character_name:
            return system_prompt
        return (
            f"{system_prompt}\n\n"
            "ACTING CHARACTER CONTEXT:\n"
            f"- Acting player character for this turn: {actor_character_name}\n"
            "- Narrate this turn from that acting character's declared action."
        )

    def _extract_intent(
        self,
        user_input: str,
        current_state: WorldState,
        llm_context: dict,
        system_prompt: str,
    ) -> PlayerIntentExtraction:
        return self.llm.extract_player_intent(
            user_input=user_input,
            state_json=current_state.model_dump_json(),
            context_json=json.dumps(llm_context),
            system_prompt=system_prompt,
        )

    def _packed_llm_context(
        self,
        db: Session,
        campaign: Campaign,
        base_context: dict,
        state: WorldState,
        actor_character_name: str | None,
        user_input: str,
    ) -> dict:
        learned_item_relevance = self._learned_item_relevance_for_context(
            db=db,
            state=state,
            user_input=user_input,
        )
        learned_effect_relevance = self._learned_effect_relevance_for_context(
            db=db,
            state=state,
            user_input=user_input,
        )
        packed = self.context_builder.pack_for_llm(
            base_context=base_context,
            state=state,
            actor_character_name=actor_character_name,
            user_input=user_input,
            learned_item_relevance=learned_item_relevance,
            learned_effect_relevance=learned_effect_relevance,
            long_term_memory=self._long_term_memory_for_context(
                db, campaign, max_entries=settings.context_memory_max_entries
            ),
            max_facts=settings.context_max_facts,
            recent_turns=settings.context_recent_turns,
            turn_line_max_chars=settings.context_turn_line_max_chars,
        )
        active_ruleset = self._ruleset_for_campaign(db, campaign)
        if active_ruleset:
            packed["game_ruleset"] = {
                "key": active_ruleset.key,
                "name": active_ruleset.name,
                "system": active_ruleset.system,
                "version": active_ruleset.version,
                "summary": active_ruleset.summary,
                "rules_json": active_ruleset.rules_json,
            }
        packed["rulebook_context"] = self._rulebook_context_for_prompt(db, campaign, user_input, limit=3)
        packed["item_knowledge"] = self._item_knowledge_for_context(db, state, user_input)
        packed["effect_knowledge"] = self._effect_knowledge_for_context(db, state, user_input)
        return packed

    @staticmethod
    def _summarize_turns_for_memory(turns: list[TurnLog], max_lines: int = 8) -> str:
        if not turns:
            return ""
        lines: list[str] = []
        for t in turns[: max(1, max_lines)]:
            actor = (t.actor or "unknown").strip()
            user_input = re.sub(r"\s+", " ", (t.user_input or "").strip())
            narration = re.sub(r"\s+", " ", (t.narration or "").strip())
            if len(user_input) > 120:
                user_input = user_input[:117].rstrip() + "..."
            if len(narration) > 120:
                narration = narration[:117].rstrip() + "..."
            lines.append(f"{actor}: {user_input} -> {narration}")
        summary = " | ".join(lines)
        return summary[:1200]

    def _refresh_long_term_memory(self, db: Session, campaign: Campaign) -> None:
        chunk_size = max(1, int(settings.context_memory_summary_turns))
        last_summary = (
            db.query(CampaignMemorySummary)
            .filter(CampaignMemorySummary.campaign_id == campaign.id)
            .order_by(CampaignMemorySummary.end_turn_id.desc())
            .first()
        )
        next_start = (last_summary.end_turn_id + 1) if last_summary else 1
        turns = (
            db.query(TurnLog)
            .filter(TurnLog.campaign_id == campaign.id, TurnLog.id >= next_start)
            .order_by(TurnLog.id.asc())
            .limit(chunk_size)
            .all()
        )
        if len(turns) < chunk_size:
            return
        summary_text = self._summarize_turns_for_memory(turns)
        if not summary_text:
            return
        db.add(
            CampaignMemorySummary(
                campaign_id=campaign.id,
                start_turn_id=turns[0].id,
                end_turn_id=turns[-1].id,
                summary=summary_text,
                created_at=datetime.utcnow(),
            )
        )
        db.commit()

    def _long_term_memory_for_context(self, db: Session, campaign: Campaign, max_entries: int = 5) -> list[dict]:
        rows = (
            db.query(CampaignMemorySummary)
            .filter(CampaignMemorySummary.campaign_id == campaign.id)
            .order_by(CampaignMemorySummary.id.desc())
            .limit(max(1, max_entries))
            .all()
        )
        rows = list(reversed(rows))
        return [
            {
                "start_turn_id": r.start_turn_id,
                "end_turn_id": r.end_turn_id,
                "summary": r.summary,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    @classmethod
    def _relevance_context_tags(cls, scene: str, user_input: str) -> set[str]:
        token_text = f"{scene or ''} {user_input or ''}".lower()
        tags: set[str] = set()
        for tag, hints in cls.RELEVANCE_TAG_HINTS.items():
            if any(re.search(rf"\b{re.escape(h)}\b", token_text) for h in hints):
                tags.add(tag)
        if not tags:
            tags.add("general")
        return tags

    @classmethod
    def _intent_item_keys(cls, intent: PlayerIntentExtraction) -> set[str]:
        keys: set[str] = set()
        for inv in intent.inventory:
            key = cls._normalize_item_key(inv.item_key)
            if key:
                keys.add(key)
        return keys

    @staticmethod
    def _normalize_effect_key(raw: str) -> str:
        text = raw.strip().lower()
        text = re.sub(r"^(?:the|a|an)\s+", "", text)
        text = re.sub(r"[^a-z0-9\s_\-']", "", text)
        text = re.sub(r"\s+", "_", text).strip("_")
        return text

    @classmethod
    def _intent_effect_keys(cls, intent: PlayerIntentExtraction) -> set[str]:
        keys: set[str] = set()
        for cmd in intent.commands:
            if cmd.type in {"add_effect", "remove_effect"} and cmd.key:
                key = cls._normalize_effect_key(cmd.key)
                if key:
                    keys.add(key)
        return keys

    @classmethod
    def _intent_relevance_scores(cls, intent: PlayerIntentExtraction, entity_type: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for signal in intent.relevance_signals:
            if signal.entity_type != entity_type:
                continue
            key = signal.key.strip().lower()
            if not key:
                continue
            if entity_type == "item":
                key = cls._normalize_item_key(key)
            elif entity_type == "effect":
                key = cls._normalize_effect_key(key)
            out[key] = max(out.get(key, 0.0), float(signal.score))
        return out

    @classmethod
    def _command_item_keys(cls, commands: list[Command]) -> set[str]:
        keys: set[str] = set()
        for cmd in commands:
            if cmd.type in {"add_item", "remove_item", "set_item_state"}:
                key = cls._normalize_item_key(cmd.key or "")
                if key:
                    keys.add(key)
        return keys

    @classmethod
    def _command_effect_keys(cls, commands: list[Command]) -> set[str]:
        keys: set[str] = set()
        for cmd in commands:
            if cmd.type in {"add_effect", "remove_effect"}:
                key = cls._normalize_effect_key(cmd.key or "")
                if key:
                    keys.add(key)
        return keys

    @classmethod
    def _items_from_user_text_fallback(cls, user_input: str) -> set[str]:
        keys: set[str] = set()
        keys |= cls._inventory_pull_items(user_input)
        pickup = cls._pickup_item_mentioned(user_input)
        if pickup:
            keys.add(cls._normalize_item_key(pickup))
        for key, _qty in cls._inventory_add_items(user_input):
            keys.add(cls._normalize_item_key(key))
        return {k for k in keys if k}

    def _record_learned_relevance(
        self,
        db: Session,
        *,
        scene: str,
        user_input: str,
        intent: PlayerIntentExtraction | None,
        accepted_commands: list[Command] | None,
        turn_log_id: int | None = None,
    ) -> None:
        if db is None:
            return
        tags = self._relevance_context_tags(scene, user_input)
        item_keys: set[str] = set()
        effect_keys: set[str] = set()
        item_signal_scores: dict[str, float] = {}
        effect_signal_scores: dict[str, float] = {}
        if intent is not None:
            item_keys |= self._intent_item_keys(intent)
            effect_keys |= self._intent_effect_keys(intent)
            item_signal_scores = self._intent_relevance_scores(intent, "item")
            effect_signal_scores = self._intent_relevance_scores(intent, "effect")
        if accepted_commands:
            item_keys |= self._command_item_keys(accepted_commands)
            effect_keys |= self._command_effect_keys(accepted_commands)
        if not item_keys:
            item_keys |= self._items_from_user_text_fallback(user_input)
        if not item_keys and not effect_keys:
            return

        now = datetime.utcnow()
        try:
            for item_key in sorted(item_keys):
                signal_score = float(item_signal_scores.get(item_key, 0.0))
                if item_signal_scores and signal_score < 0.2:
                    continue
                for tag in sorted(tags):
                    row = (
                        db.query(GlobalLearnedRelevance)
                        .filter(
                            GlobalLearnedRelevance.item_key == item_key,
                            GlobalLearnedRelevance.context_tag == tag,
                        )
                        .one_or_none()
                    )
                    if row is None:
                        row = GlobalLearnedRelevance(
                            item_key=item_key,
                            context_tag=tag,
                            interaction_count=1,
                            score=0.2 + (0.3 * signal_score),
                            created_at=now,
                            updated_at=now,
                        )
                        db.add(row)
                    else:
                        row.interaction_count += 1
                        # Saturating incremental gain keeps repeated interactions useful but bounded.
                        base_inc = 0.2 + (0.3 * signal_score)
                        row.score = min(5.0, float(row.score) + max(0.05, base_inc / (1.0 + row.interaction_count * 0.25)))
                        row.updated_at = now
                self._record_item_knowledge(
                    db=db,
                    item_key=item_key,
                    user_input=user_input,
                    scene=scene,
                    turn_log_id=turn_log_id,
                    now=now,
                )
            for effect_key in sorted(effect_keys):
                signal_score = float(effect_signal_scores.get(effect_key, 0.0))
                if effect_signal_scores and signal_score < 0.2:
                    continue
                for tag in sorted(tags):
                    row = (
                        db.query(GlobalEffectRelevance)
                        .filter(
                            GlobalEffectRelevance.effect_key == effect_key,
                            GlobalEffectRelevance.context_tag == tag,
                        )
                        .one_or_none()
                    )
                    if row is None:
                        row = GlobalEffectRelevance(
                            effect_key=effect_key,
                            context_tag=tag,
                            interaction_count=1,
                            score=0.2 + (0.3 * signal_score),
                            created_at=now,
                            updated_at=now,
                        )
                        db.add(row)
                    else:
                        row.interaction_count += 1
                        base_inc = 0.2 + (0.3 * signal_score)
                        row.score = min(5.0, float(row.score) + max(0.05, base_inc / (1.0 + row.interaction_count * 0.25)))
                        row.updated_at = now
                self._record_effect_knowledge(
                    db=db,
                    effect_key=effect_key,
                    user_input=user_input,
                    scene=scene,
                    turn_log_id=turn_log_id,
                    now=now,
                )
        except SQLAlchemyError:
            return

    @classmethod
    def _infer_item_semantics(cls, item_key: str, user_input: str, scene: str) -> tuple[str, str]:
        text = f"{item_key} {user_input} {scene}".lower()
        non_portable_hints = {"ruins", "building", "house", "shop", "city", "town", "castle", "tower", "mountain"}
        plant_hints = {"tree", "oak", "pine", "sapling"}
        if any(h in text for h in non_portable_hints):
            return "structure", "non_portable"
        if any(h in text for h in plant_hints):
            return "plant", "non_portable"
        return "item", "portable"

    def _record_item_knowledge(
        self,
        db: Session,
        item_key: str,
        user_input: str,
        scene: str,
        turn_log_id: int | None,
        now: datetime,
    ) -> None:
        row = db.query(ItemKnowledge).filter(ItemKnowledge.item_key == item_key).one_or_none()
        canonical_name = item_key.replace("_", " ").strip().title()
        object_type, portability = self._infer_item_semantics(item_key, user_input, scene)
        obs_text = self._clip_text(f"Input: {user_input} | Scene: {scene}", 320)
        if row is None:
            row = ItemKnowledge(
                item_key=item_key,
                canonical_name=canonical_name,
                object_type=object_type,
                portability=portability,
                summary=f"Observed in play: {canonical_name}.",
                aliases=[canonical_name.lower(), item_key],
                properties={},
                observation_count=1,
                confidence=0.2,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        else:
            row.observation_count += 1
            row.updated_at = now
            row.confidence = min(1.0, float(row.confidence) + 0.02)
            if row.object_type in {"", "unknown"}:
                row.object_type = object_type
            if row.portability in {"", "unknown"}:
                row.portability = portability
            aliases = [str(a).strip().lower() for a in list(row.aliases or []) if str(a).strip()]
            for alias in {canonical_name.lower(), item_key}:
                if alias not in aliases:
                    aliases.append(alias)
            row.aliases = aliases[:12]
            if not row.summary:
                row.summary = f"Observed in play: {canonical_name}."

        db.add(
            ItemObservation(
                item_key=item_key,
                turn_log_id=turn_log_id,
                observation_text=obs_text,
                created_at=now,
            )
        )

    @classmethod
    def _infer_effect_category(cls, effect_key: str, user_input: str, scene: str) -> str:
        text = f"{effect_key} {user_input} {scene}".lower()
        magical_hints = {"magic", "arcane", "spell", "curse", "hex", "mana", "enchanted", "blessed"}
        physical_hints = {"poison", "bleed", "burn", "fracture", "wound", "stun", "disease", "frostbite"}
        if any(h in text for h in magical_hints):
            return "magical"
        if any(h in text for h in physical_hints):
            return "physical"
        return "misc"

    def _record_effect_knowledge(
        self,
        db: Session,
        effect_key: str,
        user_input: str,
        scene: str,
        turn_log_id: int | None,
        now: datetime,
    ) -> None:
        row = db.query(EffectKnowledge).filter(EffectKnowledge.effect_key == effect_key).one_or_none()
        canonical_name = effect_key.replace("_", " ").strip().title()
        category = self._infer_effect_category(effect_key, user_input, scene)
        obs_text = self._clip_text(f"Input: {user_input} | Scene: {scene}", 320)
        if row is None:
            row = EffectKnowledge(
                effect_key=effect_key,
                canonical_name=canonical_name,
                category=category,
                summary=f"Observed in play: {canonical_name}.",
                aliases=[canonical_name.lower(), effect_key],
                properties={},
                observation_count=1,
                confidence=0.2,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        else:
            row.observation_count += 1
            row.updated_at = now
            row.confidence = min(1.0, float(row.confidence) + 0.02)
            if row.category in {"", "misc"}:
                row.category = category
            aliases = [str(a).strip().lower() for a in list(row.aliases or []) if str(a).strip()]
            for alias in {canonical_name.lower(), effect_key}:
                if alias not in aliases:
                    aliases.append(alias)
            row.aliases = aliases[:12]
            if not row.summary:
                row.summary = f"Observed in play: {canonical_name}."
        db.add(
            EffectObservation(
                effect_key=effect_key,
                turn_log_id=turn_log_id,
                observation_text=obs_text,
                created_at=now,
            )
        )

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        s = (text or "").strip()
        if len(s) <= limit:
            return s
        return s[: max(0, limit - 3)].rstrip() + "..."

    def _learned_item_relevance_for_context(
        self,
        db: Session | None,
        state: WorldState,
        user_input: str,
    ) -> dict[str, float]:
        if db is None:
            return {}
        tags = self._relevance_context_tags(state.scene, user_input)
        try:
            rows = (
                db.query(GlobalLearnedRelevance)
                .filter(GlobalLearnedRelevance.context_tag.in_(list(tags)))
                .all()
            )
        except SQLAlchemyError:
            return {}
        out: dict[str, float] = {}
        for row in rows:
            key = self._normalize_item_key(row.item_key)
            if not key:
                continue
            out[key] = float(out.get(key, 0.0) + float(row.score))
        return out

    def _learned_effect_relevance_for_context(
        self,
        db: Session | None,
        state: WorldState,
        user_input: str,
    ) -> dict[str, float]:
        if db is None:
            return {}
        tags = self._relevance_context_tags(state.scene, user_input)
        try:
            rows = (
                db.query(GlobalEffectRelevance)
                .filter(GlobalEffectRelevance.context_tag.in_(list(tags)))
                .all()
            )
        except SQLAlchemyError:
            return {}
        out: dict[str, float] = {}
        for row in rows:
            key = self._normalize_effect_key(row.effect_key)
            if not key:
                continue
            out[key] = float(out.get(key, 0.0) + float(row.score))
        return out

    def _item_knowledge_for_context(self, db: Session | None, state: WorldState, user_input: str) -> list[dict]:
        if db is None:
            return []
        keys = self._items_from_user_text_fallback(user_input)
        for char in state.party.values():
            for key in list(char.inventory.keys())[:12]:
                keys.add(self._normalize_item_key(key))
        keys = {k for k in keys if k}
        if not keys:
            return []
        try:
            rows = db.query(ItemKnowledge).filter(ItemKnowledge.item_key.in_(list(keys))).limit(20).all()
        except SQLAlchemyError:
            return []
        return [
            {
                "item_key": r.item_key,
                "canonical_name": r.canonical_name,
                "object_type": r.object_type,
                "portability": r.portability,
                "rarity": r.rarity,
                "summary": r.summary,
                "aliases": list(r.aliases or []),
                "properties": dict(r.properties or {}),
                "observation_count": r.observation_count,
                "confidence": float(r.confidence),
            }
            for r in rows
        ]

    def _effect_knowledge_for_context(self, db: Session | None, state: WorldState, user_input: str) -> list[dict]:
        if db is None:
            return []
        keys: set[str] = set()
        for char in state.party.values():
            for effect in list(char.effects)[:12]:
                keys.add(self._normalize_effect_key(effect.key))
        # Also allow typed mentions in raw input.
        for token in re.findall(r"[a-z][a-z0-9_\-']+", user_input.lower()):
            if token.endswith(("poison", "burn", "curse", "stun", "bleed", "bless")):
                keys.add(self._normalize_effect_key(token))
        keys = {k for k in keys if k}
        if not keys:
            return []
        try:
            rows = db.query(EffectKnowledge).filter(EffectKnowledge.effect_key.in_(list(keys))).limit(20).all()
        except SQLAlchemyError:
            return []
        return [
            {
                "effect_key": r.effect_key,
                "canonical_name": r.canonical_name,
                "category": r.category,
                "summary": r.summary,
                "aliases": list(r.aliases or []),
                "properties": dict(r.properties or {}),
                "observation_count": r.observation_count,
                "confidence": float(r.confidence),
            }
            for r in rows
        ]

    @staticmethod
    def _is_narrative_only_intent(intent: PlayerIntentExtraction) -> bool:
        if intent.inventory:
            return False
        return all(cmd.type == "narrate" for cmd in intent.commands)

    def _should_bypass_llm_review(
        self, campaign: Campaign, intent: PlayerIntentExtraction, runtime_constraints: list[str]
    ) -> bool:
        return (
            settings.story_fast_review_bypass
            and campaign.mode == "story"
            and not runtime_constraints
            and self._is_narrative_only_intent(intent)
        )

    def _should_skip_llm_review_after_precheck(self, user_input: str, candidate: str, review_payload: dict) -> bool:
        if not settings.review_precheck_enabled:
            return False
        if self._should_apply_reviewer_rewrite(review_payload):
            return False
        if self._contains_non_english_script(candidate) and not self._user_requested_non_english(user_input):
            return False
        return True

    @staticmethod
    def _normalize_for_repeat_check(text: str) -> str:
        t = re.sub(r"\s+", " ", (text or "").strip().lower())
        t = re.sub(r"[`*_~]+", "", t)
        return t

    def _previous_narration_for_same_input(self, user_input: str, context_window: dict) -> str | None:
        target = self._normalize_for_repeat_check(user_input)
        if not target:
            return None
        turns = list(context_window.get("recent_turns", []) or [])
        for row in reversed(turns):
            prior_input = self._normalize_for_repeat_check(str(row.get("user_input", "") or ""))
            if prior_input != target:
                continue
            prior_narr = str(row.get("narration", "") or "").strip()
            if prior_narr:
                return prior_narr
        return None

    def _is_reused_narration(self, candidate: str, previous: str | None) -> bool:
        if not previous:
            return False
        return self._normalize_for_repeat_check(candidate) == self._normalize_for_repeat_check(previous)

    def _force_new_narration_variant(
        self,
        campaign: Campaign,
        user_input: str,
        state: WorldState,
        context_window: dict,
        system_prompt: str,
        previous_narration: str,
    ) -> str:
        rewrite_prompt = (
            f"{system_prompt}\n\n"
            "NO-REPEAT NARRATION MODE:\n"
            "The player repeated an earlier input. Generate a fresh, materially different narration.\n"
            "Do not reuse or closely paraphrase the prior narration."
        )
        repair_context = {
            **context_window,
            "repeat_guard": {
                "user_input": user_input,
                "forbidden_prior_narration": previous_narration,
            },
        }
        rewritten = self.llm.generate(
            user_input=user_input,
            state_json=state.model_dump_json(),
            mode=campaign.mode,
            context_json=json.dumps(repair_context),
            system_prompt=rewrite_prompt,
        )
        text = rewritten.narration.strip()
        if text and not self._is_reused_narration(text, previous_narration):
            return text
        return self._build_story_continuation_failure_narration(user_input, state)

    def _review_and_repair_narration(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        user_input: str,
        intent: PlayerIntentExtraction,
        state: WorldState,
        context_window: dict,
        system_prompt: str,
        initial_narration: str,
        max_attempts: int = 1,
        enable_llm_review: bool = True,
    ) -> tuple[str, dict]:
        candidate = initial_narration.strip() or "The moment hangs in uncertainty."
        prior_narration_same_input = self._previous_narration_for_same_input(user_input, context_window)
        if not enable_llm_review:
            candidate = self._strip_system_prompt_leakage(candidate)
            candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
            candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
            if self._is_reused_narration(candidate, prior_narration_same_input):
                candidate = self._force_new_narration_variant(
                    campaign, user_input, state, context_window, system_prompt, prior_narration_same_input or ""
                )
            review_payload = self._apply_local_probability_check(
                {
                    "plausible": True,
                    "breaks_pc_autonomy": False,
                    "violations": [],
                    "revised_narration": candidate,
                    "input_aligned": True,
                    "alignment_score": 1.0,
                },
                user_input,
                candidate,
            )
            if self._fails_input_probability_check(user_input, candidate):
                candidate = self._build_story_continuation_failure_narration(user_input, state)
                candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
                review_payload = self._apply_local_probability_check(review_payload, user_input, candidate)
            return candidate, review_payload

        # Deterministic pre-processing to catch common issues before costly LLM review.
        candidate = self._strip_system_prompt_leakage(candidate)
        candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
        candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
        precheck_payload = self._apply_local_probability_check(
            {
                "plausible": True,
                "breaks_pc_autonomy": False,
                "violations": [],
                "revised_narration": candidate,
                "input_aligned": True,
                "alignment_score": 1.0,
            },
            user_input,
            candidate,
        )
        if self._is_reused_narration(candidate, prior_narration_same_input):
            precheck_payload["plausible"] = False
            precheck_payload["violations"] = [*list(precheck_payload.get("violations", [])), "repeat_response"]
        if self._should_skip_llm_review_after_precheck(user_input, candidate, precheck_payload):
            precheck_payload["precheck_skipped_llm_review"] = True
            return candidate, precheck_payload

        review = self.llm.review_output(
            user_input=user_input,
            narration=candidate,
            state_json=state.model_dump_json(),
            context_json=json.dumps(context_window),
            system_prompt=system_prompt,
        )
        review_payload = self._apply_local_probability_check(review.model_dump(), user_input, candidate)
        if self._should_apply_reviewer_rewrite(review_payload):
            candidate = (review_payload.get("revised_narration", "") or candidate).strip() or candidate
        candidate = self._strip_system_prompt_leakage(candidate)
        candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
        candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)

        for _ in range(max_attempts):
            needs_repair = (
                (not bool(review_payload.get("plausible", True)))
                or bool(review_payload.get("breaks_pc_autonomy", False))
                or ("system_prompt" in review_payload.get("violations", []))
                or ("input_mismatch" in review_payload.get("violations", []))
                or ("repeat_response" in review_payload.get("violations", []))
            )
            if not needs_repair:
                return candidate, review_payload

            repair_context = {
                **context_window,
                "failed_review": review_payload,
                "failed_narration": candidate,
                "rewrite_requirements": [
                    "Produce a materially different narration.",
                    "Keep narration plausible to current state and inventory.",
                    "Do not puppeteer any other player character.",
                    "If the attempted action is impossible, narrate attempt + failure.",
                    "Narration must directly address the player's actual input.",
                    "Return empty commands unless truly required.",
                ],
            }
            repair_prompt = (
                f"{system_prompt}\n\n"
                "NARRATION REPAIR MODE:\n"
                "The previous narration failed validation. Regenerate a corrected narration that obeys all rules."
            )
            repaired = self.llm.generate(
                user_input=user_input,
                state_json=state.model_dump_json(),
                mode=campaign.mode,
                context_json=json.dumps(repair_context),
                system_prompt=repair_prompt,
            )
            new_candidate = repaired.narration.strip()
            if new_candidate and new_candidate != candidate:
                candidate = new_candidate

            candidate = self._strip_system_prompt_leakage(candidate)
            candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
            candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
            review = self.llm.review_output(
                user_input=user_input,
                narration=candidate,
                state_json=state.model_dump_json(),
                context_json=json.dumps(context_window),
                system_prompt=system_prompt,
            )
            review_payload = self._apply_local_probability_check(review.model_dump(), user_input, candidate)
            if self._is_reused_narration(candidate, prior_narration_same_input):
                review_payload["plausible"] = False
                review_payload["violations"] = [*list(review_payload.get("violations", [])), "repeat_response"]
            revised = review.revised_narration.strip()
            if revised and self._should_apply_reviewer_rewrite(review_payload):
                candidate = revised
                candidate = self._strip_system_prompt_leakage(candidate)
                candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
                candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)

        if (not bool(review_payload.get("plausible", True))) or bool(review_payload.get("breaks_pc_autonomy", False)):
            final_repair_context = {
                **context_window,
                "failed_review": review_payload,
                "failed_narration": candidate,
                "rewrite_requirements": [
                    "This is the final rewrite attempt.",
                    "Narration must directly respond to the player's specific input.",
                    "Keep continuity with scene and recent conversation.",
                    "Do not include system/rules text.",
                    "Do not break player autonomy.",
                    "If action is impossible, narrate an in-world failed attempt that still advances the scene.",
                    "No generic placeholder phrasing.",
                ],
            }
            final_repair_prompt = (
                f"{system_prompt}\n\n"
                "FINAL NARRATION RECOVERY MODE:\n"
                "Generate one corrected narration that obeys all constraints and keeps the story moving."
            )
            repaired = self.llm.generate(
                user_input=user_input,
                state_json=state.model_dump_json(),
                mode=campaign.mode,
                context_json=json.dumps(final_repair_context),
                system_prompt=final_repair_prompt,
            )
            repaired_text = repaired.narration.strip()
            if repaired_text:
                candidate = repaired_text
                candidate = self._strip_system_prompt_leakage(candidate)
                candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
                candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
                final_review = self.llm.review_output(
                    user_input=user_input,
                    narration=candidate,
                    state_json=state.model_dump_json(),
                    context_json=json.dumps(context_window),
                    system_prompt=system_prompt,
                )
                review_payload = self._apply_local_probability_check(final_review.model_dump(), user_input, candidate)
                revised = str(review_payload.get("revised_narration", "")).strip()
                if revised and self._should_apply_reviewer_rewrite(review_payload):
                    candidate = revised
                    candidate = self._strip_system_prompt_leakage(candidate)
                    candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
                    candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)

            # Emergency fallback only when generation yielded nothing.
            if not candidate.strip():
                candidate = self._build_story_continuation_failure_narration(user_input, state)
                candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)

        if self._is_reused_narration(candidate, prior_narration_same_input):
            candidate = self._force_new_narration_variant(
                campaign, user_input, state, context_window, system_prompt, prior_narration_same_input or ""
            )
            candidate = self._strip_system_prompt_leakage(candidate)
            candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
            candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
            review_payload = self._apply_local_probability_check(review_payload, user_input, candidate)

        # Final hard guard: never return low-information/mismatched output like "The scene shifts."
        if self._fails_input_probability_check(user_input, candidate):
            candidate = self._build_story_continuation_failure_narration(user_input, state)
            candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
            review_payload = self._apply_local_probability_check(review_payload, user_input, candidate)

        if self._contains_non_english_script(candidate) and not self._user_requested_non_english(user_input):
            rewrite_prompt = (
                f"{system_prompt}\n\n"
                "ENGLISH-ONLY REWRITE MODE:\n"
                "Rewrite the narration into natural English. Preserve meaning, constraints, and tone."
            )
            rewritten = self.llm.generate(
                user_input=user_input,
                state_json=state.model_dump_json(),
                mode=campaign.mode,
                context_json=json.dumps(context_window),
                system_prompt=rewrite_prompt,
            )
            if rewritten.narration.strip():
                candidate = rewritten.narration.strip()
            if self._contains_non_english_script(candidate):
                candidate = self._build_story_continuation_failure_narration(user_input, state)
            candidate = self._strip_system_prompt_leakage(candidate)
            candidate = self._enforce_other_player_agency_on_narration(db, campaign, actor, candidate)
            candidate = self._enforce_infeasible_intent_on_narration(candidate, intent)
        return candidate, review_payload

    def _runtime_constraints_from_intent(
        self,
        db: Session,
        campaign: Campaign,
        player: Player,
        current_state: WorldState,
        intent: PlayerIntentExtraction,
        user_input: str = "",
    ) -> list[str]:
        constraints: list[str] = []
        actor_name = None
        if db is not None and campaign is not None and player is not None:
            try:
                actor_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
            except Exception:
                actor_name = None
        actor = current_state.party.get(actor_name) if actor_name else None
        party_names_ci = {name.lower(): name for name in current_state.party.keys()}
        purchase_attempt = self._is_purchase_attempt(user_input)
        has_funds = self._has_currency(actor.inventory) if actor else False
        in_shop = self._is_shop_context(current_state.scene, user_input)
        if actor:
            missing_item = self._first_missing_personal_item(user_input, set(actor.inventory.keys()))
            if missing_item:
                check = self._find_intent_feasibility_check(intent, "use", missing_item, None)
                if check is None:
                    assessed = self.llm.assess_inventory_action_feasibility(
                        action="use",
                        item_key=missing_item,
                        scene=current_state.scene,
                        user_input=user_input,
                        state_json=current_state.model_dump_json(),
                        context_json=json.dumps(
                            {
                                "actor_name": actor_name or "",
                                "actor_inventory": actor.inventory,
                                "inventory_count": int(actor.inventory.get(missing_item, 0)),
                            }
                        ),
                    )
                    is_possible = bool(assessed.get("is_possible", False))
                    reason = str(assessed.get("reason", "") or "").strip()
                    if int(actor.inventory.get(missing_item, 0)) <= 0:
                        is_possible = False
                        if not reason:
                            reason = f"'{missing_item}' is not currently in the actor inventory."
                    if not is_possible:
                        constraints.append(
                            f"Can the actor use '{missing_item}' from inventory right now? -> NO. "
                            f"{reason or 'Usage is not possible under current inventory state.'}"
                        )
        for check in intent.feasibility_checks:
            if self._requires_explicit_feasibility_resolution(check):
                continue
            if not check.is_possible:
                msg = check.reason.strip() or "Action is not possible in the current state."
                constraints.append(f"{check.question} -> NO. {msg}")
        for row in intent.inventory:
            item_key = row.item_key.strip().lower()
            if not item_key:
                continue
            check = self._find_intent_feasibility_check(intent, row.action, item_key, row.target_character)
            if check and check.portability == "non_portable":
                constraints.append(
                    f"'{item_key}' is non-portable and cannot be placed into inventory. "
                    "Narrate an attempted action that fails in-world."
                )
                continue
            # Legacy deterministic guard only when LLM intent did not provide portability.
            if check is None and row.action in {"add", "pickup"} and self._is_deterministically_non_portable(item_key):
                constraints.append(
                    f"'{item_key}' is non-portable and cannot be placed into inventory. "
                    "Narrate an attempted action that fails in-world."
                )
                continue
            # Impossible rule: player characters cannot be inventory items.
            if row.action == "add":
                target_name = (row.target_character or "").strip().lower()
                if (target_name and target_name in party_names_ci) or item_key in party_names_ci:
                    name = party_names_ci.get(target_name) or party_names_ci.get(item_key) or "that character"
                    constraints.append(
                        f"It is impossible to put player character '{name}' into any inventory. "
                        "Narrate the shove/attempt failing and keep character placement unchanged."
                    )
                    continue
                if check and check.requires_payment and check.has_required_funds is False:
                    constraints.append(
                        f"Transaction failed: insufficient funds for '{item_key}'"
                        + (f" ({check.cost_amount} {check.currency})" if check.cost_amount is not None and check.currency else "")
                        + ". Narrate the attempt and refusal/payment failure."
                    )
                    continue
                if check and check.requires_payment and check.cost_amount is not None and check.currency and actor:
                    have_amt = self._currency_amount(actor.inventory, check.currency)
                    need_amt = int(check.cost_amount)
                    if have_amt < need_amt:
                        constraints.append(
                            f"Transaction failed: insufficient funds for '{item_key}' ({need_amt} {check.currency}). "
                            f"Actor has {have_amt} {check.currency}."
                        )
                        continue
                if in_shop and not purchase_attempt:
                    theft = bool(check.would_be_theft) if check is not None else True
                    if theft:
                        constraints.append(
                            f"Taking '{item_key}' without buying it in this shop context would be theft. "
                            "Narrate a denied attempt or consequences, not a free inventory gain."
                        )
                        continue
                # Legacy transaction fallback if model omitted explicit transaction fields.
                if check is None and purchase_attempt and not has_funds:
                    constraints.append(
                        f"Purchase attempt failed: you do not have currency in your inventory to buy '{item_key}'. "
                        "Narrate the attempt and refusal/payment failure."
                    )
                    continue
            if row.action in {"use", "remove"} and row.owner in {"self", "unknown"} and actor:
                have = int(actor.inventory.get(item_key, 0))
                need = int(row.quantity)
                if have < need:
                    constraints.append(
                        f"Can the actor use '{item_key}' from inventory right now? -> NO. "
                        f"Inventory count is {have}, required is {need}."
                    )
            if row.action == "steal":
                if not row.target_character:
                    continue
                target_name = self._resolve_party_name_ci(current_state, row.target_character)
                if not target_name:
                    continue
                target = current_state.party[target_name]
                if target.inventory.get(item_key, 0) < row.quantity:
                    constraints.append(
                        f"{target_name} does not have a '{item_key}' in their inventory, "
                        "so theft of that item fails."
                    )
        return constraints

    @staticmethod
    def _is_intent_action_feasible(intent: PlayerIntentExtraction, action: str, item_key: str, target: str | None) -> bool:
        action_l = action.lower()
        item_l = item_key.lower()
        target_l = (target or "").lower()
        matching = [
            c
            for c in intent.feasibility_checks
            if c.action.lower() == action_l
            and c.item_key.lower() == item_l
            and ((c.target_character or "").lower() == target_l or not (c.target_character or ""))
        ]
        if not matching:
            return True
        return all(c.is_possible for c in matching)

    @staticmethod
    def _find_intent_feasibility_check(
        intent: PlayerIntentExtraction, action: str, item_key: str, target: str | None
    ):
        action_l = action.lower()
        item_l = item_key.lower()
        target_l = (target or "").lower()
        for c in intent.feasibility_checks:
            if c.action.lower() != action_l:
                continue
            if c.item_key.lower() != item_l:
                continue
            c_target = (c.target_character or "").lower()
            if c_target and c_target != target_l:
                continue
            return c
        return None

    @staticmethod
    def _is_deterministically_non_portable(item_key: str) -> bool:
        _, portability = LLMAdapter._classify_object(item_key)
        return portability == "non_portable"

    def _commands_from_intent(
        self, db: Session, campaign: Campaign, player: Player, intent: PlayerIntentExtraction
    ) -> list[Command]:
        actor_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        commands: list[Command] = []

        for cmd in intent.commands:
            normalized = cmd.model_copy(deep=True)
            if normalized.type in {"add_item", "remove_item", "set_stat", "adjust_hp", "set_item_state", "add_effect", "remove_effect"}:
                if not normalized.target and actor_name:
                    normalized.target = actor_name
            commands.append(normalized)

        if commands:
            return commands

        # Fallback path for extractors that only fill `inventory`.
        for row in intent.inventory:
            item_key = row.item_key.strip().lower()
            if not item_key:
                continue
            qty = max(1, row.quantity)
            if row.action == "add" and row.owner in {"self", "unknown"} and actor_name:
                commands.append(Command(type="add_item", target=actor_name, key=item_key, amount=qty))
            elif row.action == "remove" and row.owner in {"self", "unknown"} and actor_name:
                commands.append(Command(type="remove_item", target=actor_name, key=item_key, amount=qty))
        return commands

    def _resolve_inventory_actions_from_intent(
        self,
        db: Session,
        campaign: Campaign,
        player: Player,
        current_state: WorldState,
        intent: PlayerIntentExtraction,
        user_input: str = "",
    ) -> dict | None:
        actor_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        if not actor_name or actor_name not in current_state.party:
            return None

        commands: list[Command] = []
        added_items: list[tuple[str, int]] = []
        in_shop = self._is_shop_context(current_state.scene, user_input)
        purchase_attempt = self._is_purchase_attempt(user_input)
        for row in intent.inventory:
            item_key = row.item_key.strip().lower()
            if not item_key:
                continue
            if row.action == "add" and row.owner in {"self", "unknown"}:
                check = self._find_intent_feasibility_check(intent, "add", item_key, row.target_character)
                if check is not None and not check.is_possible:
                    continue
                if check is not None and check.requires_payment and check.has_required_funds is False:
                    continue
                if check is None:
                    assessed = self.llm.assess_inventory_action_feasibility(
                        action="add",
                        item_key=item_key,
                        scene=current_state.scene,
                        user_input=user_input,
                        state_json=current_state.model_dump_json(),
                        context_json="{}",
                    )
                    if not bool(assessed.get("is_possible", False)):
                        continue
                    if in_shop and not purchase_attempt and bool(assessed.get("would_be_theft", True)):
                        continue
                if check is not None and in_shop and not purchase_attempt and bool(check.would_be_theft):
                    continue
                if check is not None and check.requires_payment:
                    currency = str(check.currency or "").strip().lower()
                    cost = int(check.cost_amount) if check.cost_amount is not None else None
                    if currency and cost is not None:
                        have_amt = self._currency_amount(current_state.party[actor_name].inventory, currency)
                        if have_amt < cost:
                            continue
                qty = max(1, row.quantity)
                commands.append(Command(type="add_item", target=actor_name, key=item_key, amount=qty))
                added_items.append((item_key, qty))
                if check is not None and check.requires_payment and check.cost_amount is not None and check.currency:
                    commands.append(
                        Command(
                            type="remove_item",
                            target=actor_name,
                            key=self._normalize_currency_item_key(check.currency),
                            amount=max(1, int(check.cost_amount)),
                        )
                    )
            elif row.action == "pickup" and row.owner in {"scene", "unknown"}:
                check = self._find_intent_feasibility_check(intent, "pickup", item_key, row.target_character)
                if check is None or self._requires_explicit_feasibility_resolution(check):
                    assessed = self.llm.assess_inventory_action_feasibility(
                        action="pickup",
                        item_key=item_key,
                        scene=current_state.scene,
                        user_input=user_input,
                        state_json=current_state.model_dump_json(),
                        context_json="{}",
                    )
                    if not bool(assessed.get("is_possible", False)):
                        continue
                elif check is not None and not check.is_possible:
                    continue
                qty = max(1, row.quantity)
                commands.append(Command(type="add_item", target=actor_name, key=item_key, amount=qty))
                added_items.append((item_key, qty))

        if not commands:
            return None
        pretty_items = ", ".join(f"{qty} {key.replace('_', ' ')}" for key, qty in added_items)
        return {
            "handled": True,
            "accepted": commands,
            "rejected": [],
            "narration": f"You place {pretty_items} into your inventory.",
        }

    def get_or_create_campaign(
        self,
        db: Session,
        thread_id: str,
        mode: str = "dnd",
        thread_name: str | None = None,
    ) -> Campaign:
        self.seed_default_auth(db)
        self.seed_default_agency_rules(db)
        self.seed_default_gameplay_knowledge(db)

        if settings.gameplay_use_db_api:
            try:
                row = self.db_api.campaign_by_thread(thread_id)
                if row is None:
                    generated_state = self.llm.generate_world_seed(mode=mode)
                    row = self.db_api.upsert_campaign_by_thread(
                        thread_id=thread_id,
                        mode=mode,
                        state=generated_state.model_dump(),
                    )
                campaign = db.query(Campaign).filter(Campaign.id == int(row["id"])).one_or_none() if row else None
                if campaign:
                    name = (thread_name or "").strip()
                    if name:
                        existing = self.list_rules(db, campaign).get("thread_name", "").strip()
                        if existing != name:
                            self.set_rule(db, campaign, "thread_name", name)
                    return campaign
            except Exception:
                # Fallback to local DB path if API is unavailable or not aligned.
                pass

        campaign = db.query(Campaign).filter(Campaign.discord_thread_id == thread_id).one_or_none()
        if campaign:
            name = (thread_name or "").strip()
            if name:
                existing = self.list_rules(db, campaign).get("thread_name", "").strip()
                if existing != name:
                    self.set_rule(db, campaign, "thread_name", name)
            return campaign

        generated_state = self.llm.generate_world_seed(mode=mode)
        campaign = Campaign(discord_thread_id=thread_id, mode=mode, state=generated_state.model_dump())
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        name = (thread_name or "").strip()
        if name:
            self.set_rule(db, campaign, "thread_name", name)
        return campaign

    def seed_default_agency_rules(self, db: Session) -> None:
        for rule_id, body in DEFAULT_RULE_BLOCKS.items():
            exists = db.query(AgencyRuleBlock).filter(AgencyRuleBlock.rule_id == rule_id).one_or_none()
            if exists:
                continue
            title = "Unknown"
            priority = "high"
            for line in body.splitlines():
                if line.startswith("TITLE:"):
                    title = line.removeprefix("TITLE:").strip()
                if line.startswith("PRIORITY:"):
                    priority = line.removeprefix("PRIORITY:").strip()
            db.add(
                AgencyRuleBlock(rule_id=rule_id, title=title, priority=priority, body=body, is_enabled=True)
            )
        db.commit()

    def ensure_player(self, db: Session, campaign: Campaign, actor_id: str, display_name: str) -> Player:
        player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor_id)
            .one_or_none()
        )
        if player:
            if player.display_name != display_name:
                player.display_name = display_name
                db.add(player)
                db.commit()
            return player

        player = Player(campaign_id=campaign.id, discord_user_id=actor_id, display_name=display_name)
        db.add(player)
        db.commit()
        db.refresh(player)
        return player

    def player_character_name(self, db: Session, campaign_id: int, player_id: int) -> str | None:
        row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign_id, Character.player_id == player_id)
            .one_or_none()
        )
        return row.name if row else None

    def ensure_default_character_for_player(self, db: Session, campaign: Campaign, player: Player) -> tuple[str, bool]:
        current_state = WorldState.model_validate(campaign.state)
        row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign.id, Character.player_id == player.id)
            .one_or_none()
        )
        if row and row.name in current_state.party:
            return row.name, False

        base_name = (player.display_name or "").strip() or f"Player_{player.id}"
        base_name = re.sub(r"\s+", "_", base_name)
        base_name = re.sub(r"[^A-Za-z0-9_\-']", "", base_name).strip("_") or f"Player_{player.id}"

        existing_name = row.name if row else None
        unique_name = base_name
        idx = 2
        while unique_name in current_state.party and unique_name != existing_name:
            unique_name = f"{base_name}_{idx}"
            idx += 1

        default_char = CharacterState(
            name=unique_name,
            description="A newly arrived adventurer.",
            hp=10,
            max_hp=10,
            stats={"str": 10, "dex": 10, "int": 10},
            inventory={},
            item_states={},
            effects=[],
        )

        if existing_name and existing_name in current_state.party and existing_name != unique_name:
            del current_state.party[existing_name]
        current_state.party[unique_name] = default_char
        campaign.state = current_state.model_dump()
        db.add(campaign)

        if row:
            row.name = unique_name
            row.role = "player_character"
            row.hp = default_char.hp
            row.max_hp = default_char.max_hp
            row.stats = default_char.stats
            row.item_states = default_char.item_states
            row.effects = []
            db.add(row)
        else:
            db.add(
                Character(
                    campaign_id=campaign.id,
                    player_id=player.id,
                    name=unique_name,
                    role="player_character",
                    hp=default_char.hp,
                    max_hp=default_char.max_hp,
                    stats=default_char.stats,
                    item_states=default_char.item_states,
                    effects=[],
                )
            )

        db.commit()
        return unique_name, True

    def register_character_from_description(
        self,
        db: Session,
        campaign: Campaign,
        player: Player,
        description: str,
    ) -> CharacterState:
        current_state = WorldState.model_validate(campaign.state)
        suggested = self.llm.generate_character_from_description(description, fallback_name=player.display_name)

        char_row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign.id, Character.player_id == player.id)
            .one_or_none()
        )
        existing_name = char_row.name if char_row else None

        unique_name = suggested.name
        idx = 2
        while unique_name in current_state.party and unique_name != existing_name:
            unique_name = f"{suggested.name}_{idx}"
            idx += 1
        suggested.name = unique_name

        if existing_name and existing_name in current_state.party and existing_name != unique_name:
            del current_state.party[existing_name]
        current_state.party[unique_name] = suggested
        campaign.state = current_state.model_dump()
        db.add(campaign)

        if char_row:
            char_row.name = unique_name
            char_row.role = "player_character"
            char_row.hp = suggested.hp
            char_row.max_hp = suggested.max_hp
            char_row.stats = suggested.stats
            char_row.item_states = suggested.item_states
            char_row.effects = [e.model_dump() for e in suggested.effects]
            db.add(char_row)
        else:
            db.add(
                Character(
                    campaign_id=campaign.id,
                    player_id=player.id,
                    name=unique_name,
                    role="player_character",
                    hp=suggested.hp,
                    max_hp=suggested.max_hp,
                    stats=suggested.stats,
                    item_states=suggested.item_states,
                    effects=[e.model_dump() for e in suggested.effects],
                )
            )
        self.sync_inventory_for_player(db, player, current_state)
        db.commit()
        return suggested

    def delete_player_character(self, db: Session, campaign: Campaign, player: Player) -> tuple[bool, str]:
        current_state = WorldState.model_validate(campaign.state)
        row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign.id, Character.player_id == player.id)
            .one_or_none()
        )
        if not row:
            return False, "You don't have a linked character to delete."

        char_name = row.name
        if char_name in current_state.party:
            del current_state.party[char_name]
            campaign.state = current_state.model_dump()
            db.add(campaign)

        for inv in db.query(InventoryItem).filter(InventoryItem.player_id == player.id).all():
            db.delete(inv)
        db.delete(row)
        db.commit()
        return True, f"Deleted character '{char_name}'."

    def build_campaign_system_prompt(self, db: Session, campaign: Campaign) -> str:
        rules = self.list_rules(db, campaign)
        character_instructions = rules.get("character_instructions", "")
        custom_directives = rules.get("system_prompt_custom", "")

        player_controlled_names = [
            row.name for row in db.query(Character).filter(Character.campaign_id == campaign.id).all() if row.player_id
        ]
        if player_controlled_names:
            names_csv = ", ".join(sorted(player_controlled_names))
            custom_directives = (
                f"{custom_directives}\n\n"
                "Player-controlled characters (never puppeteer these): "
                f"{names_csv}\n"
                "Only narrate or resolve the acting player's declared actions."
            ).strip()

        explicit_ids = rules.get("agency_rule_ids", "").strip()
        if explicit_ids:
            rule_ids = [rid.strip() for rid in explicit_ids.split(",") if rid.strip() in self.available_rule_ids(db)]
        else:
            default_profile = "minimal" if campaign.mode == "story" else "balanced"
            profile = rules.get("agency_rule_profile", default_profile).strip().lower()
            rule_ids = rule_ids_for_profile(profile)

        db_blocks = {
            row.rule_id: row.body
            for row in db.query(AgencyRuleBlock).filter(AgencyRuleBlock.is_enabled.is_(True)).all()
        }

        return build_system_prompt(
            character_instructions=character_instructions,
            custom_directives=custom_directives,
            rule_ids=rule_ids,
            rule_blocks=db_blocks,
        )

    def available_rule_profiles(self) -> list[str]:
        return list(RULE_PROFILES.keys())

    def available_rule_ids(self, db: Session) -> list[str]:
        return [r.rule_id for r in db.query(AgencyRuleBlock).filter(AgencyRuleBlock.is_enabled.is_(True)).all()]

    def list_rules(self, db: Session, campaign: Campaign) -> dict[str, str]:
        if settings.gameplay_use_db_api:
            try:
                return self.db_api.campaign_rules(int(campaign.id))
            except Exception:
                pass
        rows = db.query(CampaignRule).filter(CampaignRule.campaign_id == campaign.id).all()
        return {r.rule_key: r.rule_value for r in rows}

    def list_game_rulesets(self, db: Session, enabled_only: bool = False) -> list[GameRuleset]:
        q = db.query(GameRuleset)
        if enabled_only:
            q = q.filter(GameRuleset.is_enabled.is_(True))
        return q.order_by(GameRuleset.system.asc(), GameRuleset.name.asc()).all()

    def upsert_game_ruleset(
        self,
        db: Session,
        *,
        key: str,
        name: str,
        system: str,
        version: str,
        summary: str,
        is_official: bool,
        is_enabled: bool,
        rules_json: dict | None = None,
    ) -> tuple[bool, str]:
        k = self._normalize_ruleset_key(key)
        if not k:
            return False, "Ruleset key is required."
        row = db.query(GameRuleset).filter(GameRuleset.key == k).one_or_none()
        if not row:
            row = GameRuleset(
                key=k,
                name=name.strip() or k,
                system=system.strip().lower() or "dnd",
                version=version.strip(),
                summary=summary.strip(),
                is_official=bool(is_official),
                is_enabled=bool(is_enabled),
                rules_json=dict(rules_json or {}),
            )
            db.add(row)
        else:
            row.name = name.strip() or row.name
            row.system = system.strip().lower() or row.system
            row.version = version.strip()
            row.summary = summary.strip()
            row.is_official = bool(is_official)
            row.is_enabled = bool(is_enabled)
            row.rules_json = dict(rules_json or {})
            row.updated_at = datetime.utcnow()
        db.commit()
        return True, k

    def list_rulebooks(self, db: Session, enabled_only: bool = False) -> list[Rulebook]:
        q = db.query(Rulebook)
        if enabled_only:
            q = q.filter(Rulebook.is_enabled.is_(True))
        return q.order_by(Rulebook.system.asc(), Rulebook.title.asc()).all()

    def upsert_rulebook(
        self,
        db: Session,
        *,
        slug: str,
        title: str,
        system: str,
        version: str,
        source: str,
        summary: str,
        is_enabled: bool,
    ) -> tuple[bool, str]:
        s = self._normalize_ruleset_key(slug)
        if not s:
            return False, "Rulebook slug is required."
        row = db.query(Rulebook).filter(Rulebook.slug == s).one_or_none()
        if not row:
            row = Rulebook(
                slug=s,
                title=title.strip() or s,
                system=system.strip().lower() or "dnd",
                version=version.strip(),
                source=source.strip(),
                summary=summary.strip(),
                is_enabled=bool(is_enabled),
            )
            db.add(row)
        else:
            row.title = title.strip() or row.title
            row.system = system.strip().lower() or row.system
            row.version = version.strip()
            row.source = source.strip()
            row.summary = summary.strip()
            row.is_enabled = bool(is_enabled)
            row.updated_at = datetime.utcnow()
        db.commit()
        return True, s

    def upsert_rulebook_entry(
        self,
        db: Session,
        *,
        rulebook_slug: str,
        entry_key: str,
        title: str,
        section: str,
        page_ref: str,
        tags: list[str] | None,
        content: str,
    ) -> tuple[bool, str]:
        book_slug = self._normalize_ruleset_key(rulebook_slug)
        key_norm = self._normalize_ruleset_key(entry_key)
        if not book_slug or not key_norm:
            return False, "Rulebook slug and entry key are required."
        book = db.query(Rulebook).filter(Rulebook.slug == book_slug).one_or_none()
        if not book:
            return False, f"Rulebook '{book_slug}' not found."
        tags_norm = [t.strip().lower() for t in (tags or []) if t.strip()]
        search_blob = " ".join([title, section, content, " ".join(tags_norm)]).lower()
        row = (
            db.query(RulebookEntry)
            .filter(RulebookEntry.rulebook_id == book.id, RulebookEntry.entry_key == key_norm)
            .one_or_none()
        )
        if not row:
            row = RulebookEntry(
                rulebook_id=book.id,
                entry_key=key_norm,
                title=title.strip() or key_norm,
                section=section.strip(),
                page_ref=page_ref.strip(),
                tags=tags_norm,
                content=content.strip(),
                searchable_text=search_blob,
            )
            db.add(row)
        else:
            row.title = title.strip() or row.title
            row.section = section.strip()
            row.page_ref = page_ref.strip()
            row.tags = tags_norm
            row.content = content.strip()
            row.searchable_text = search_blob
            row.updated_at = datetime.utcnow()
        db.commit()
        return True, key_norm

    def _ruleset_for_campaign(self, db: Session, campaign: Campaign) -> GameRuleset | None:
        rules = self.list_rules(db, campaign)
        explicit = self._normalize_ruleset_key(rules.get("game_ruleset", ""))
        if explicit:
            row = db.query(GameRuleset).filter(GameRuleset.key == explicit, GameRuleset.is_enabled.is_(True)).one_or_none()
            if row:
                return row
        fallback = "story-freeform" if campaign.mode == "story" else "dnd5e-2014"
        return db.query(GameRuleset).filter(GameRuleset.key == fallback, GameRuleset.is_enabled.is_(True)).one_or_none()

    def get_campaign_ruleset(self, db: Session, campaign: Campaign) -> GameRuleset | None:
        return self._ruleset_for_campaign(db, campaign)

    def set_campaign_ruleset(self, db: Session, campaign: Campaign, ruleset_key: str) -> tuple[bool, str]:
        key = self._normalize_ruleset_key(ruleset_key)
        row = db.query(GameRuleset).filter(GameRuleset.key == key, GameRuleset.is_enabled.is_(True)).one_or_none()
        if not row:
            return False, f"Unknown or disabled ruleset: {ruleset_key}"
        self.set_rule(db, campaign, "game_ruleset", row.key)
        return True, row.key

    def search_rulebook_entries(
        self, db: Session, query: str, *, ruleset_key: str = "", limit: int = 5
    ) -> list[dict[str, str | list[str]]]:
        q = query.strip().lower()
        if not q:
            return []
        tokens = self._tokenize_for_search(q)
        if not tokens:
            return []
        limit = max(1, min(20, int(limit)))
        book_q = db.query(Rulebook).filter(Rulebook.is_enabled.is_(True))
        if ruleset_key:
            ruleset = db.query(GameRuleset).filter(GameRuleset.key == self._normalize_ruleset_key(ruleset_key)).one_or_none()
            if ruleset:
                book_q = book_q.filter(Rulebook.system == ruleset.system)
        books = book_q.all()
        if not books:
            return []
        by_id = {b.id: b for b in books}
        rows = db.query(RulebookEntry).filter(RulebookEntry.rulebook_id.in_(list(by_id.keys()))).limit(400).all()
        scored: list[tuple[int, RulebookEntry]] = []
        for row in rows:
            blob = (row.searchable_text or "").lower()
            if not blob:
                blob = f"{row.title} {row.section} {row.content} {' '.join(list(row.tags or []))}".lower()
            token_hits = sum(1 for t in tokens if t in blob)
            if token_hits <= 0:
                continue
            scored.append((token_hits, row))
        scored.sort(key=lambda p: p[0], reverse=True)
        out: list[dict[str, str | list[str]]] = []
        for _score, row in scored[:limit]:
            book = by_id[row.rulebook_id]
            out.append(
                {
                    "rulebook": book.title,
                    "entry_key": row.entry_key,
                    "title": row.title,
                    "section": row.section,
                    "page_ref": row.page_ref,
                    "content": row.content,
                    "tags": list(row.tags or []),
                }
            )
        return out

    def rule_lookup_for_campaign(self, db: Session, campaign: Campaign, query: str, limit: int = 3) -> list[dict]:
        ruleset = self._ruleset_for_campaign(db, campaign)
        return self.search_rulebook_entries(
            db,
            query,
            ruleset_key=ruleset.key if ruleset else "",
            limit=limit,
        )

    def _rulebook_context_for_prompt(self, db: Session, campaign: Campaign, user_input: str, limit: int = 3) -> list[dict]:
        snippets = self.rule_lookup_for_campaign(db, campaign, user_input, limit=limit)
        out: list[dict] = []
        for s in snippets:
            content = str(s.get("content", "")).strip()
            if len(content) > 220:
                content = content[:217].rstrip() + "..."
            out.append(
                {
                    "source": f"{s.get('rulebook', '')} {s.get('page_ref', '')}".strip(),
                    "title": s.get("title", ""),
                    "section": s.get("section", ""),
                    "content": content,
                }
            )
        return out

    def roll_dice(self, expression: str) -> tuple[bool, dict]:
        raw = (expression or "").strip()
        m = self.DICE_EXPR_RE.match(raw)
        if not m:
            return False, {"error": "Invalid roll format. Use examples like `d20`, `2d6+3`, `adv d20+5`."}
        adv_mode_raw, count_txt, sides_txt, mod_txt = m.groups()
        count = int(count_txt) if count_txt else 1
        sides = int(sides_txt)
        modifier = int(mod_txt.replace(" ", "")) if mod_txt else 0
        if count < 1 or count > 100:
            return False, {"error": "Dice count must be between 1 and 100."}
        if sides < 2 or sides > 1000:
            return False, {"error": "Dice sides must be between 2 and 1000."}
        adv_mode = "none"
        if adv_mode_raw:
            adv_mode = "advantage" if adv_mode_raw.lower().startswith("adv") else "disadvantage"

        if adv_mode != "none":
            if not (count == 1 and sides == 20):
                return False, {"error": "Advantage/disadvantage currently supports `d20` rolls only."}
            r1 = 1 + secrets.randbelow(20)
            r2 = 1 + secrets.randbelow(20)
            picked = max(r1, r2) if adv_mode == "advantage" else min(r1, r2)
            total = picked + modifier
            normalized = f"{'adv' if adv_mode == 'advantage' else 'dis'} d20{modifier:+d}" if modifier else (
                "adv d20" if adv_mode == "advantage" else "dis d20"
            )
            return True, {
                "expression": raw,
                "normalized_expression": normalized,
                "rolls": [r1, r2],
                "picked": picked,
                "roll_count": 1,
                "sides": 20,
                "modifier": modifier,
                "advantage_mode": adv_mode,
                "total": total,
            }

        rolls = [1 + secrets.randbelow(sides) for _ in range(count)]
        subtotal = sum(rolls)
        total = subtotal + modifier
        normalized = f"{count if count != 1 else ''}d{sides}{modifier:+d}" if modifier else f"{count if count != 1 else ''}d{sides}"
        return True, {
            "expression": raw,
            "normalized_expression": normalized,
            "rolls": rolls,
            "roll_count": count,
            "sides": sides,
            "modifier": modifier,
            "advantage_mode": "none",
            "subtotal": subtotal,
            "total": total,
        }

    def log_dice_roll(
        self,
        db: Session,
        *,
        campaign: Campaign | None,
        actor_discord_user_id: str,
        actor_display_name: str,
        roll_data: dict,
    ) -> None:
        db.add(
            DiceRollLog(
                campaign_id=campaign.id if campaign else None,
                actor_discord_user_id=actor_discord_user_id,
                actor_display_name=actor_display_name,
                expression=str(roll_data.get("expression", "")),
                normalized_expression=str(roll_data.get("normalized_expression", "")),
                sides=int(roll_data.get("sides", 20) or 20),
                roll_count=int(roll_data.get("roll_count", 1) or 1),
                modifier=int(roll_data.get("modifier", 0) or 0),
                advantage_mode=str(roll_data.get("advantage_mode", "none")),
                total=int(roll_data.get("total", 0) or 0),
                breakdown=dict(roll_data),
            )
        )
        db.commit()

    @staticmethod
    def _normalize_ai_raw_output_payload(payload: dict | None) -> dict:
        candidate = dict(payload or {})
        schema_version = int(candidate.get("schema_version", 1) or 1)
        if schema_version <= 1:
            if not str(candidate.get("source", "")).strip():
                candidate["source"] = "migrated_unknown"
            candidate["_migrated_from_schema_version"] = schema_version
            candidate["schema_version"] = GameService.AI_RAW_OUTPUT_SCHEMA_VERSION
        else:
            candidate.setdefault("schema_version", schema_version)
            if not str(candidate.get("source", "")).strip():
                candidate["source"] = "unknown"
        return candidate

    @staticmethod
    def deserialize_ai_raw_output(raw: str | dict | None) -> dict:
        try:
            if isinstance(raw, dict):
                parsed = dict(raw)
            elif isinstance(raw, str):
                parsed = json.loads(raw) if raw.strip() else {}
            elif raw is None:
                parsed = {}
            else:
                parsed = {"value": str(raw)}
            if not isinstance(parsed, dict):
                parsed = {"value": str(parsed)}
            candidate = GameService._normalize_ai_raw_output_payload(parsed)
            AIRawOutputEnvelope.model_validate(candidate)
            return candidate
        except Exception as exc:  # noqa: BLE001
            fallback = {
                "schema_version": GameService.AI_RAW_OUTPUT_SCHEMA_VERSION,
                "source": "serialization_error",
                "error": str(exc),
                "raw_payload": raw if isinstance(raw, dict) else {"value": str(raw)},
            }
            return fallback

    @staticmethod
    def serialize_ai_raw_output(payload: dict | None) -> str:
        candidate = GameService.deserialize_ai_raw_output(payload or {})
        try:
            AIRawOutputEnvelope.model_validate(candidate)
        except ValidationError as exc:
            candidate = {
                "schema_version": GameService.AI_RAW_OUTPUT_SCHEMA_VERSION,
                "source": "serialization_error",
                "error": str(exc),
                "raw_payload": payload if isinstance(payload, dict) else {"value": str(payload)},
            }
        return json.dumps(candidate)

    def reserve_discord_message_idempotency(
        self,
        db: Session,
        *,
        campaign: Campaign | None,
        discord_message_id: str,
        actor_discord_user_id: str,
    ) -> bool:
        msg_id = (discord_message_id or "").strip()
        actor_id = (actor_discord_user_id or "").strip()
        if not msg_id or not actor_id:
            return False
        if settings.gameplay_use_db_api:
            try:
                return self.db_api.reserve_idempotency(
                    campaign_id=(campaign.id if campaign else None),
                    discord_message_id=msg_id,
                    actor_discord_user_id=actor_id,
                )
            except Exception:
                pass
        exists = (
            db.query(ProcessedDiscordMessage)
            .filter(ProcessedDiscordMessage.discord_message_id == msg_id)
            .one_or_none()
        )
        if exists:
            return False
        db.add(
            ProcessedDiscordMessage(
                campaign_id=campaign.id if campaign else None,
                discord_message_id=msg_id,
                actor_discord_user_id=actor_id,
            )
        )
        db.commit()
        return True

    def report_player_issue(
        self,
        db: Session,
        *,
        campaign: Campaign,
        actor_discord_user_id: str,
        actor_display_name: str,
        message: str,
    ) -> tuple[bool, str]:
        issue_text = (message or "").strip()
        if not issue_text:
            return False, "Issue text is required."
        try:
            state = WorldState.model_validate(campaign.state)
            player = (
                db.query(Player)
                .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor_discord_user_id)
                .one_or_none()
            )
            actor_character_name = (
                self.player_character_name(db, campaign_id=campaign.id, player_id=player.id) if player else None
            )
            actor_character = state.party.get(actor_character_name) if actor_character_name else None
            recent_turns = (
                db.query(TurnLog)
                .filter(TurnLog.campaign_id == campaign.id)
                .order_by(TurnLog.id.desc())
                .limit(8)
                .all()
            )
            recent_payload = [
                {
                    "turn_id": t.id,
                    "actor": t.actor,
                    "user_input": t.user_input,
                    "narration": t.narration,
                    "created_at": t.created_at.isoformat() if t.created_at else "",
                }
                for t in reversed(recent_turns)
            ]
            actor_snapshot = None
            if actor_character:
                actor_snapshot = {
                    "name": actor_character.name,
                    "description": actor_character.description,
                    "hp": actor_character.hp,
                    "max_hp": actor_character.max_hp,
                    "stats": actor_character.stats,
                    "inventory": actor_character.inventory,
                    "effects": [e.model_dump() for e in actor_character.effects],
                }
            audit_row = AdminAuditLog(
                actor_source="discord",
                actor_id=actor_discord_user_id,
                actor_display=actor_display_name[:128],
                action="player_issue_reported",
                target=f"campaign:{campaign.id}",
                audit_metadata={
                    "campaign_id": campaign.id,
                    "thread_id": campaign.discord_thread_id,
                    "mode": campaign.mode,
                    "issue_text": issue_text[:4000],
                    "actor_character_name": actor_character_name or "",
                    "actor_character_snapshot": actor_snapshot or {},
                    "recent_turns": recent_payload,
                },
                created_at=datetime.utcnow(),
            )
            db.add(audit_row)
            db.commit()
            return True, f"Issue report logged (id={audit_row.id})."
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            return False, f"Failed to report issue: {exc}"

    def audit_admin_action(
        self,
        db: Session,
        *,
        actor_source: str,
        actor_id: str,
        actor_display: str,
        action: str,
        target: str = "",
        metadata: dict | None = None,
    ) -> None:
        try:
            db.add(
                AdminAuditLog(
                    actor_source=(actor_source or "unknown").strip().lower()[:32],
                    actor_id=(actor_id or "").strip()[:128],
                    actor_display=(actor_display or "").strip()[:128],
                    action=(action or "unknown").strip()[:128],
                    target=(target or "").strip()[:256],
                    audit_metadata=dict(metadata or {}),
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    def set_rule(self, db: Session, campaign: Campaign, key: str, value: str) -> None:
        if settings.gameplay_use_db_api:
            try:
                self.db_api.set_campaign_rule(int(campaign.id), key, value)
                return
            except Exception:
                pass
        row = (
            db.query(CampaignRule)
            .filter(CampaignRule.campaign_id == campaign.id, CampaignRule.rule_key == key)
            .one_or_none()
        )
        if row:
            row.rule_value = value
            db.add(row)
        else:
            db.add(CampaignRule(campaign_id=campaign.id, rule_key=key, rule_value=value))
        db.commit()

    @staticmethod
    def _hash_password(password: str, salt_hex: str) -> str:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000)
        return digest.hex()

    def seed_default_auth(self, db: Session) -> None:
        for perm_name, desc in self.DEFAULT_PERMISSIONS.items():
            row = db.query(AuthPermission).filter(AuthPermission.name == perm_name).one_or_none()
            if row:
                if row.description != desc:
                    row.description = desc
                    row.updated_at = datetime.utcnow()
                    db.add(row)
            else:
                db.add(AuthPermission(name=perm_name, description=desc))
        db.flush()

        perm_by_name = {p.name: p for p in db.query(AuthPermission).all()}
        for role_name, role_meta in self.DEFAULT_ROLES.items():
            role = db.query(AuthRole).filter(AuthRole.name == role_name).one_or_none()
            if role:
                role.description = str(role_meta.get("description", role.description))
                role.updated_at = datetime.utcnow()
                db.add(role)
            else:
                role = AuthRole(name=role_name, description=str(role_meta.get("description", "")))
                db.add(role)
                db.flush()
            desired_perms = {perm_by_name[pn].id for pn in set(role_meta.get("permissions", set())) if pn in perm_by_name}
            existing_links = db.query(AuthRolePermission).filter(AuthRolePermission.role_id == role.id).all()
            existing_perm_ids = {x.permission_id for x in existing_links}
            for perm_id in desired_perms - existing_perm_ids:
                db.add(AuthRolePermission(role_id=role.id, permission_id=perm_id))
            for link in existing_links:
                if link.permission_id not in desired_perms:
                    db.delete(link)

        username = settings.auth_bootstrap_admin_username.strip()
        password = settings.auth_bootstrap_admin_password.strip()
        if username and password:
            admin = db.query(AuthUser).filter(AuthUser.username == username).one_or_none()
            if not admin:
                ok, _msg = self.auth_create_user(
                    db,
                    username=username,
                    password=password,
                    display_name="Bootstrap Admin",
                    roles=["admin"],
                )
                if ok:
                    db.flush()
        db.commit()

    def auth_create_user(
        self,
        db: Session,
        *,
        username: str,
        password: str,
        display_name: str = "",
        roles: list[str] | None = None,
        discord_user_id: str | None = None,
    ) -> tuple[bool, str]:
        uname = username.strip().lower()
        if not uname or not password:
            return False, "Username and password are required."
        if db.query(AuthUser).filter(AuthUser.username == uname).one_or_none():
            return False, "Username already exists."
        salt = secrets.token_hex(16)
        hashed = self._hash_password(password, salt)
        user = AuthUser(
            username=uname,
            password_hash=hashed,
            password_salt=salt,
            display_name=display_name.strip() or uname,
            discord_user_id=discord_user_id.strip() if discord_user_id else None,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(user)
        db.flush()
        assigned_roles = roles or ["player"]
        for role_name in assigned_roles:
            role = db.query(AuthRole).filter(AuthRole.name == role_name.strip().lower()).one_or_none()
            if not role:
                continue
            db.add(AuthUserRole(user_id=user.id, role_id=role.id))
        db.commit()
        return True, "User created."

    def auth_set_user_password(self, db: Session, username: str, new_password: str) -> tuple[bool, str]:
        user = db.query(AuthUser).filter(AuthUser.username == username.strip().lower()).one_or_none()
        if not user:
            return False, "User not found."
        if not new_password:
            return False, "Password cannot be empty."
        salt = secrets.token_hex(16)
        user.password_salt = salt
        user.password_hash = self._hash_password(new_password, salt)
        user.updated_at = datetime.utcnow()
        db.add(user)
        db.commit()
        return True, "Password updated."

    def auth_authenticate_user(self, db: Session, username: str, password: str) -> AuthUser | None:
        user = db.query(AuthUser).filter(AuthUser.username == username.strip().lower()).one_or_none()
        if not user or not user.is_active:
            return None
        expected = self._hash_password(password, user.password_salt)
        if not hmac.compare_digest(expected, user.password_hash):
            return None
        return user

    def auth_assign_role(self, db: Session, username: str, role_name: str) -> tuple[bool, str]:
        user = db.query(AuthUser).filter(AuthUser.username == username.strip().lower()).one_or_none()
        role = db.query(AuthRole).filter(AuthRole.name == role_name.strip().lower()).one_or_none()
        if not user or not role:
            return False, "User or role not found."
        existing = (
            db.query(AuthUserRole).filter(AuthUserRole.user_id == user.id, AuthUserRole.role_id == role.id).one_or_none()
        )
        if existing:
            return True, "Role already assigned."
        db.add(AuthUserRole(user_id=user.id, role_id=role.id))
        db.commit()
        return True, "Role assigned."

    def auth_link_discord_user(self, db: Session, username: str, discord_user_id: str) -> tuple[bool, str]:
        user = db.query(AuthUser).filter(AuthUser.username == username.strip().lower()).one_or_none()
        if not user:
            return False, "User not found."
        user.discord_user_id = discord_user_id.strip()
        user.updated_at = datetime.utcnow()
        db.add(user)
        db.commit()
        return True, "Discord account linked."

    def auth_user_has_permission(self, db: Session, user_id: int, permission_name: str) -> bool:
        perm = db.query(AuthPermission).filter(AuthPermission.name == permission_name.strip().lower()).one_or_none()
        if not perm:
            return False
        role_ids = [r.role_id for r in db.query(AuthUserRole).filter(AuthUserRole.user_id == user_id).all()]
        if not role_ids:
            return False
        link = (
            db.query(AuthRolePermission)
            .filter(AuthRolePermission.role_id.in_(role_ids), AuthRolePermission.permission_id == perm.id)
            .first()
        )
        return bool(link)

    def auth_discord_user_has_permission(self, db: Session, discord_user_id: str, permission_name: str) -> bool:
        user = db.query(AuthUser).filter(AuthUser.discord_user_id == discord_user_id, AuthUser.is_active.is_(True)).one_or_none()
        if user:
            return self.auth_user_has_permission(db, user.id, permission_name)
        return False

    def auth_can(self, db: Session, discord_user_id: str, permission_name: str) -> bool:
        if self.auth_discord_user_has_permission(db, discord_user_id, permission_name):
            return True
        return self.is_sys_admin(db, discord_user_id)

    def auth_list_users(self, db: Session) -> list[dict]:
        users = db.query(AuthUser).order_by(AuthUser.username.asc()).all()
        role_map = {r.id: r.name for r in db.query(AuthRole).all()}
        links = db.query(AuthUserRole).all()
        roles_by_user: dict[int, list[str]] = {}
        for link in links:
            roles_by_user.setdefault(link.user_id, []).append(role_map.get(link.role_id, str(link.role_id)))
        return [
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "discord_user_id": u.discord_user_id,
                "is_active": u.is_active,
                "roles": sorted(roles_by_user.get(u.id, [])),
            }
            for u in users
        ]

    @staticmethod
    def _snapshot_scopes() -> set[str]:
        return {"all", "state", "rules", "players", "characters", "turns"}

    def export_campaign_snapshot(
        self,
        db: Session,
        campaign: Campaign,
        actor_discord_user_id: str,
        scope: str = "all",
    ) -> tuple[bool, str, str]:
        scope_norm = (scope or "all").strip().lower()
        if scope_norm not in self._snapshot_scopes():
            return False, "", f"Invalid scope '{scope_norm}'. Use one of: {', '.join(sorted(self._snapshot_scopes()))}."

        payload: dict = {
            "scope": scope_norm,
            "exported_at": datetime.utcnow().isoformat(),
            "source_campaign_id": campaign.id,
            "source_thread_id": campaign.discord_thread_id,
        }
        if scope_norm in {"all", "state"}:
            payload["campaign"] = {
                "mode": campaign.mode,
                "state": campaign.state,
            }
        if scope_norm in {"all", "rules"}:
            payload["rules"] = [
                {"rule_key": r.rule_key, "rule_value": r.rule_value}
                for r in db.query(CampaignRule).filter(CampaignRule.campaign_id == campaign.id).all()
            ]
        if scope_norm in {"all", "players"}:
            payload["players"] = [
                {"discord_user_id": p.discord_user_id, "display_name": p.display_name}
                for p in db.query(Player).filter(Player.campaign_id == campaign.id).all()
            ]
        if scope_norm in {"all", "characters"}:
            players = db.query(Player).filter(Player.campaign_id == campaign.id).all()
            player_id_to_discord = {p.id: p.discord_user_id for p in players}
            payload["characters"] = [
                {
                    "name": c.name,
                    "role": c.role,
                    "hp": c.hp,
                    "max_hp": c.max_hp,
                    "stats": c.stats,
                    "item_states": c.item_states,
                    "effects": c.effects,
                    "player_discord_user_id": player_id_to_discord.get(c.player_id),
                }
                for c in db.query(Character).filter(Character.campaign_id == campaign.id).all()
            ]
        if scope_norm in {"all", "turns"}:
            payload["turns"] = [
                {
                    "actor": t.actor,
                    "user_input": t.user_input,
                    "ai_raw_output": t.ai_raw_output,
                    "accepted_commands": t.accepted_commands,
                    "rejected_commands": t.rejected_commands,
                    "narration": t.narration,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in db.query(TurnLog).filter(TurnLog.campaign_id == campaign.id).order_by(TurnLog.id.asc()).all()
            ]

        snapshot_key = f"snap_{secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:20]}"
        db.add(
            CampaignSnapshot(
                source_campaign_id=campaign.id,
                created_by_discord_user_id=actor_discord_user_id,
                scope=scope_norm,
                snapshot_key=snapshot_key,
                payload=payload,
            )
        )
        db.commit()
        return True, snapshot_key, "Snapshot created."

    def _clear_campaign_runtime_data(self, db: Session, campaign: Campaign) -> None:
        for row in db.query(InventoryItem).join(Player, InventoryItem.player_id == Player.id).filter(
            Player.campaign_id == campaign.id
        ):
            db.delete(row)
        for row in db.query(Character).filter(Character.campaign_id == campaign.id).all():
            db.delete(row)
        for row in db.query(Player).filter(Player.campaign_id == campaign.id).all():
            db.delete(row)
        for row in db.query(CampaignRule).filter(CampaignRule.campaign_id == campaign.id).all():
            db.delete(row)
        for row in db.query(TurnLog).filter(TurnLog.campaign_id == campaign.id).all():
            db.delete(row)

    def import_campaign_snapshot_to_thread(
        self,
        db: Session,
        *,
        target_thread_id: str,
        actor_discord_user_id: str,
        snapshot_key: str,
        admin_force: bool = False,
    ) -> tuple[bool, str]:
        snapshot = db.query(CampaignSnapshot).filter(CampaignSnapshot.snapshot_key == snapshot_key.strip()).one_or_none()
        if not snapshot:
            return False, "Snapshot not found."
        payload = dict(snapshot.payload or {})
        scope = str(payload.get("scope", snapshot.scope or "all")).strip().lower()
        if scope not in self._snapshot_scopes():
            return False, "Snapshot scope is invalid."
        if not admin_force and scope == "all":
            # normal import still allowed; this keeps path explicit for future constraints
            pass

        campaign = db.query(Campaign).filter(Campaign.discord_thread_id == target_thread_id).one_or_none()
        if campaign is None:
            mode = str(payload.get("campaign", {}).get("mode", "dnd"))
            campaign = Campaign(discord_thread_id=target_thread_id, mode=mode, state=WorldState().model_dump())
            db.add(campaign)
            db.flush()

        if scope == "all":
            self._clear_campaign_runtime_data(db, campaign)

        campaign_payload = payload.get("campaign", {})
        if scope in {"all", "state"} and isinstance(campaign_payload, dict):
            campaign.mode = str(campaign_payload.get("mode", campaign.mode) or campaign.mode)
            state = campaign_payload.get("state")
            if isinstance(state, dict):
                campaign.state = state
        db.add(campaign)
        db.flush()

        if scope in {"all", "rules"}:
            for row in db.query(CampaignRule).filter(CampaignRule.campaign_id == campaign.id).all():
                db.delete(row)
            for row in list(payload.get("rules", []) or []):
                if not isinstance(row, dict):
                    continue
                key = str(row.get("rule_key", "")).strip()
                if not key:
                    continue
                db.add(
                    CampaignRule(
                        campaign_id=campaign.id,
                        rule_key=key,
                        rule_value=str(row.get("rule_value", "")),
                    )
                )

        player_map: dict[str, Player] = {}
        if scope in {"all", "players", "characters"}:
            for row in db.query(InventoryItem).join(Player, InventoryItem.player_id == Player.id).filter(
                Player.campaign_id == campaign.id
            ):
                db.delete(row)
            for row in db.query(Character).filter(Character.campaign_id == campaign.id).all():
                db.delete(row)
            for row in db.query(Player).filter(Player.campaign_id == campaign.id).all():
                db.delete(row)
            db.flush()

            for row in list(payload.get("players", []) or []):
                if not isinstance(row, dict):
                    continue
                discord_user_id = str(row.get("discord_user_id", "")).strip()
                if not discord_user_id:
                    continue
                p = Player(
                    campaign_id=campaign.id,
                    discord_user_id=discord_user_id,
                    display_name=str(row.get("display_name", "") or discord_user_id),
                )
                db.add(p)
                db.flush()
                player_map[discord_user_id] = p

            for row in list(payload.get("characters", []) or []):
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name", "")).strip()
                if not name:
                    continue
                owner_discord_id = str(row.get("player_discord_user_id", "") or "").strip()
                owner = player_map.get(owner_discord_id) if owner_discord_id else None
                db.add(
                    Character(
                        campaign_id=campaign.id,
                        player_id=owner.id if owner else None,
                        name=name,
                        role=str(row.get("role", "player_character") or "player_character"),
                        hp=int(row.get("hp", 10) or 10),
                        max_hp=int(row.get("max_hp", 10) or 10),
                        stats=dict(row.get("stats", {}) or {}),
                        item_states=dict(row.get("item_states", {}) or {}),
                        effects=list(row.get("effects", []) or []),
                    )
                )
            db.flush()

            # rebuild normalized inventory rows from imported state if available
            state = WorldState.model_validate(campaign.state)
            for p in player_map.values():
                self.sync_inventory_for_player(db, p, state)
                self.sync_character_row(db, campaign, p, state)

        if scope in {"all", "turns"}:
            for row in db.query(TurnLog).filter(TurnLog.campaign_id == campaign.id).all():
                db.delete(row)
            for row in list(payload.get("turns", []) or []):
                if not isinstance(row, dict):
                    continue
                db.add(
                    TurnLog(
                        campaign_id=campaign.id,
                        actor=str(row.get("actor", "")),
                        user_input=str(row.get("user_input", "")),
                        ai_raw_output=str(row.get("ai_raw_output", "")),
                        accepted_commands=list(row.get("accepted_commands", []) or []),
                        rejected_commands=list(row.get("rejected_commands", []) or []),
                        narration=str(row.get("narration", "")),
                    )
                )

        db.commit()
        return True, f"Imported snapshot `{snapshot.snapshot_key}` into this thread."

    def list_campaign_snapshots(self, db: Session, limit: int = 50) -> list[CampaignSnapshot]:
        return db.query(CampaignSnapshot).order_by(CampaignSnapshot.id.desc()).limit(max(1, limit)).all()

    def authenticate_sys_admin(self, db: Session, discord_user_id: str, display_name: str, token: str) -> bool:
        if not settings.sys_admin_token or token != settings.sys_admin_token:
            return False

        admin = db.query(SysAdminUser).filter(SysAdminUser.discord_user_id == discord_user_id).one_or_none()
        if admin:
            admin.display_name = display_name
            admin.is_active = True
            admin.last_login_at = datetime.utcnow()
            db.add(admin)
        else:
            db.add(
                SysAdminUser(
                    discord_user_id=discord_user_id,
                    display_name=display_name,
                    is_active=True,
                    last_login_at=datetime.utcnow(),
                )
            )
        db.commit()
        return True

    def is_sys_admin(self, db: Session, discord_user_id: str) -> bool:
        admin = db.query(SysAdminUser).filter(SysAdminUser.discord_user_id == discord_user_id).one_or_none()
        return bool(admin and admin.is_active)

    def admin_list_rule_blocks(self, db: Session) -> list[AgencyRuleBlock]:
        return db.query(AgencyRuleBlock).order_by(AgencyRuleBlock.rule_id.asc()).all()

    def admin_upsert_rule_block(self, db: Session, rule_id: str, title: str, priority: str, body: str) -> None:
        row = db.query(AgencyRuleBlock).filter(AgencyRuleBlock.rule_id == rule_id).one_or_none()
        if row:
            row.title = title
            row.priority = priority
            row.body = body
            row.is_enabled = True
            row.updated_at = datetime.utcnow()
            db.add(row)
        else:
            db.add(
                AgencyRuleBlock(
                    rule_id=rule_id,
                    title=title,
                    priority=priority,
                    body=body,
                    is_enabled=True,
                )
            )
        db.commit()

    def admin_remove_rule_block(self, db: Session, rule_id: str) -> bool:
        row = db.query(AgencyRuleBlock).filter(AgencyRuleBlock.rule_id == rule_id).one_or_none()
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    def admin_set_rule_block_enabled(self, db: Session, rule_id: str, is_enabled: bool) -> bool:
        row = db.query(AgencyRuleBlock).filter(AgencyRuleBlock.rule_id == rule_id).one_or_none()
        if not row:
            return False
        row.is_enabled = is_enabled
        row.updated_at = datetime.utcnow()
        db.add(row)
        db.commit()
        return True

    def sync_inventory_for_player(self, db: Session, player: Player, state: WorldState, *, auto_commit: bool = True) -> None:
        char_name = self.player_character_name(db, campaign_id=player.campaign_id, player_id=player.id)
        if not char_name:
            return

        char = state.party.get(char_name)
        if not char:
            return

        existing_rows = db.query(InventoryItem).filter(InventoryItem.player_id == player.id).all()
        existing_map = {r.item_key: r for r in existing_rows}
        state_keys = set(char.inventory.keys())

        for item_key, quantity in char.inventory.items():
            row = existing_map.get(item_key)
            if row:
                row.quantity = quantity
                db.add(row)
            else:
                db.add(InventoryItem(player_id=player.id, item_key=item_key, quantity=quantity, item_metadata={}))

        for item_key, row in existing_map.items():
            if item_key not in state_keys:
                db.delete(row)

        if auto_commit:
            db.commit()

    def sync_character_row(
        self, db: Session, campaign: Campaign, player: Player, state: WorldState, *, auto_commit: bool = True
    ) -> None:
        char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        if not char_name or char_name not in state.party:
            return

        char_state = state.party[char_name]
        row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign.id, Character.player_id == player.id)
            .one_or_none()
        )
        if not row:
            return

        row.name = char_state.name
        row.hp = char_state.hp
        row.max_hp = char_state.max_hp
        row.stats = char_state.stats
        row.item_states = char_state.item_states
        row.effects = [e.model_dump() for e in char_state.effects]
        db.add(row)
        if auto_commit:
            db.commit()

    def apply_manual_commands(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        user_input: str,
        commands: list[Command],
    ) -> tuple[bool, str]:
        state = WorldState.model_validate(campaign.state)
        state_before = state.model_dump()
        validation = validate_commands(state, commands)
        if validation["rejected"]:
            reason = validation["rejected"][0]["reason"]
            return False, reason

        updated = apply_commands(state, validation["accepted"])
        updated = tick_effects(updated)
        state_after = updated.model_dump()
        campaign.state = updated.model_dump()

        player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor)
            .one_or_none()
        )
        if player:
            self.sync_inventory_for_player(db, player, updated, auto_commit=False)
            self.sync_character_row(db, campaign, player, updated, auto_commit=False)

        turn = TurnLog(
            campaign_id=campaign.id,
            actor=actor,
            user_input=user_input,
            ai_raw_output=self.serialize_ai_raw_output(
                {"source": "manual_command", "state_before": state_before, "state_after": state_after}
            ),
            accepted_commands=[c.model_dump() for c in validation["accepted"]],
            rejected_commands=[],
            narration="Manual state change applied.",
        )
        db.add(turn)
        db.add(campaign)
        db.commit()
        return True, "ok"

    @staticmethod
    def _extract_state_before_from_turn(turn: TurnLog) -> dict | None:
        payload = GameService.deserialize_ai_raw_output(turn.ai_raw_output)
        state_before = payload.get("state_before")
        return state_before if isinstance(state_before, dict) else None

    def _resync_all_players_to_state(self, db: Session, campaign: Campaign, state: WorldState) -> None:
        players = db.query(Player).filter(Player.campaign_id == campaign.id).all()
        for p in players:
            self.sync_inventory_for_player(db, p, state)
            self.sync_character_row(db, campaign, p, state)

    def retry_last_input_for_player(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        actor_display_name: str,
    ) -> tuple[bool, str, dict]:
        last_turn = (
            db.query(TurnLog)
            .filter(TurnLog.campaign_id == campaign.id, TurnLog.actor == actor)
            .order_by(TurnLog.id.desc())
            .one_or_none()
        )
        if not last_turn:
            return False, "No previous turn found for you in this thread.", {}

        prior_state = self._extract_state_before_from_turn(last_turn)
        if not prior_state:
            return False, "Cannot retry this turn because prior state snapshot is missing.", {}

        campaign.state = prior_state
        db.add(campaign)
        restored = WorldState.model_validate(prior_state)
        self._resync_all_players_to_state(db, campaign, restored)
        db.commit()

        narration, details = self.process_turn_routed(
            db,
            campaign=campaign,
            actor=actor,
            actor_display_name=actor_display_name,
            user_input=last_turn.user_input,
        )
        return True, narration, details

    def teach_knowledge(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        *,
        kind: str,
        key: str,
        observation: str,
    ) -> tuple[bool, str]:
        normalized_kind = kind.strip().lower()
        if normalized_kind not in {"item", "effect"}:
            return False, "Kind must be `item` or `effect`."
        normalized_key = (
            self._normalize_item_key(key) if normalized_kind == "item" else self._normalize_effect_key(key)
        )
        if not normalized_key:
            return False, "Key is required."
        if not observation.strip():
            return False, "Observation text is required."

        state = WorldState.model_validate(campaign.state)
        scene = state.scene
        now = datetime.utcnow()
        note = self._clip_text(observation, 500)
        tags = self._relevance_context_tags(scene, note)
        try:
            if normalized_kind == "item":
                self._record_item_knowledge(
                    db=db,
                    item_key=normalized_key,
                    user_input=note,
                    scene=scene,
                    turn_log_id=None,
                    now=now,
                )
                for tag in sorted(tags):
                    row = (
                        db.query(GlobalLearnedRelevance)
                        .filter(
                            GlobalLearnedRelevance.item_key == normalized_key,
                            GlobalLearnedRelevance.context_tag == tag,
                        )
                        .one_or_none()
                    )
                    if row is None:
                        db.add(
                            GlobalLearnedRelevance(
                                item_key=normalized_key,
                                context_tag=tag,
                                interaction_count=1,
                                score=0.2,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        row.interaction_count += 1
                        row.score = min(5.0, float(row.score) + 0.08)
                        row.updated_at = now
            else:
                self._record_effect_knowledge(
                    db=db,
                    effect_key=normalized_key,
                    user_input=note,
                    scene=scene,
                    turn_log_id=None,
                    now=now,
                )
                for tag in sorted(tags):
                    row = (
                        db.query(GlobalEffectRelevance)
                        .filter(
                            GlobalEffectRelevance.effect_key == normalized_key,
                            GlobalEffectRelevance.context_tag == tag,
                        )
                        .one_or_none()
                    )
                    if row is None:
                        db.add(
                            GlobalEffectRelevance(
                                effect_key=normalized_key,
                                context_tag=tag,
                                interaction_count=1,
                                score=0.2,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        row.interaction_count += 1
                        row.score = min(5.0, float(row.score) + 0.08)
                        row.updated_at = now

            turn = TurnLog(
                campaign_id=campaign.id,
                actor=actor,
                user_input=f"!teach {normalized_kind}|{normalized_key}|{note}",
                ai_raw_output=self.serialize_ai_raw_output(
                    {
                        "source": "teach_command",
                        "kind": normalized_kind,
                        "key": normalized_key,
                        "observation": note,
                    }
                ),
                accepted_commands=[],
                rejected_commands=[],
                narration=f"Learned {normalized_kind} observation for {normalized_key}.",
            )
            db.add(turn)
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            return False, f"Failed to record teaching: {exc}"
        return True, f"Learned {normalized_kind} observation for `{normalized_key}`."

    def process_turn(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        actor_display_name: str,
        user_input: str,
        correlation_id: str | None = None,
    ) -> tuple[str, dict]:
        turn_correlation_id = (correlation_id or "").strip() or self._new_turn_correlation_id()
        player = self.ensure_player(db, campaign, actor, actor_display_name)
        auto_character_name, auto_created_default_character = self.ensure_default_character_for_player(db, campaign, player)
        actor_char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        normalized_user_input = self._normalize_quoted_player_input(user_input, actor_display_name)
        current_state = WorldState.model_validate(campaign.state)
        state_before = current_state.model_dump()
        self_view = self._self_inspection_narration(current_state, actor_char_name, normalized_user_input)
        if self_view:
            turn = TurnLog(
                campaign_id=campaign.id,
                actor=actor,
                user_input=user_input,
                ai_raw_output=self.serialize_ai_raw_output(
                    {
                        "source": "deterministic_self_query",
                        "correlation_id": turn_correlation_id,
                        "actor_character_name": actor_char_name,
                        "normalized_user_input": normalized_user_input,
                    }
                ),
                accepted_commands=[],
                rejected_commands=[],
                narration=self_view,
            )
            db.add(turn)
            db.commit()
            return self_view, {
                "accepted": [],
                "rejected": [],
                "context": {"correlation_id": turn_correlation_id},
                "auto_created_default_character": auto_created_default_character,
                "auto_character_name": auto_character_name,
            }
        context_window = self.context_builder.build(db, campaign, actor_id=actor)
        context_window["correlation_id"] = turn_correlation_id
        system_prompt = self.build_campaign_system_prompt(db, campaign)
        system_prompt = self._augment_system_prompt_with_actor(system_prompt, actor_char_name)
        context_window["actor_inventory"] = self._actor_inventory_for_context(db, campaign, actor, current_state)
        context_window["actor_character_name"] = actor_char_name
        context_window["conversation_history"] = self._full_conversation_for_context(db, campaign)
        context_window["raw_user_input"] = user_input
        context_window["normalized_user_input"] = normalized_user_input
        llm_context = self._packed_llm_context(
            db, campaign, context_window, current_state, actor_char_name, normalized_user_input
        )
        intent = self._extract_intent(normalized_user_input, current_state, llm_context, system_prompt)
        runtime_constraints = self._runtime_constraints_from_intent(
            db, campaign, player, current_state, intent, user_input=normalized_user_input
        )

        inventory_resolution = self._resolve_inventory_actions_from_intent(
            db, campaign, player, current_state, intent, user_input=normalized_user_input
        )
        if inventory_resolution and inventory_resolution["handled"]:
            accepted_cmds: list[Command] = inventory_resolution["accepted"]
            new_state = apply_commands(current_state, accepted_cmds)
            new_state = tick_effects(new_state)
            campaign.state = new_state.model_dump()
            self.sync_inventory_for_player(db, player, new_state, auto_commit=False)
            self.sync_character_row(db, campaign, player, new_state, auto_commit=False)

            turn = TurnLog(
                campaign_id=campaign.id,
                actor=actor,
                user_input=user_input,
                ai_raw_output=self.serialize_ai_raw_output(
                    {
                        "source": "intent_inventory_resolver",
                        "correlation_id": turn_correlation_id,
                        "intent": intent.model_dump(),
                        "state_before": state_before,
                        "state_after": new_state.model_dump(),
                    }
                ),
                accepted_commands=[c.model_dump() for c in accepted_cmds],
                rejected_commands=[],
                narration=inventory_resolution["narration"],
            )
            db.add(turn)
            db.flush()
            self._record_learned_relevance(
                db,
                scene=current_state.scene,
                user_input=normalized_user_input,
                intent=intent,
                accepted_commands=accepted_cmds,
                turn_log_id=turn.id,
            )
            db.add(campaign)
            db.commit()
            self._refresh_long_term_memory(db, campaign)
            return inventory_resolution["narration"], {
                "accepted": [c.model_dump() for c in accepted_cmds],
                "rejected": [],
                "context": {"correlation_id": turn_correlation_id},
                "auto_created_default_character": auto_created_default_character,
                "auto_character_name": auto_character_name,
            }

        intent_commands = [
            c
            for c in self._commands_from_intent(db, campaign, player, intent)
            if c.type not in {"add_item", "remove_item"}
        ]
        intent_validation = validate_commands(current_state, intent_commands)
        intent_state = apply_commands(current_state, intent_validation["accepted"])

        if runtime_constraints:
            context_window["runtime_constraints"] = runtime_constraints
        context_window["intent"] = intent.model_dump()
        context_window["intent_validation"] = {
            "accepted": [c.model_dump() for c in intent_validation["accepted"]],
            "rejected": intent_validation["rejected"],
        }

        system_prompt = self._augment_system_prompt_with_runtime_constraints(system_prompt, runtime_constraints)
        llm_context = self._packed_llm_context(
            db, campaign, context_window, intent_state, actor_char_name, normalized_user_input
        )
        ai_response = self.llm.generate(
            user_input=normalized_user_input,
            state_json=intent_state.model_dump_json(),
            mode=campaign.mode,
            context_json=json.dumps(llm_context),
            system_prompt=system_prompt,
        )

        safe_narration = self._enforce_other_player_agency_on_narration(db, campaign, actor, ai_response.narration)
        final_narration, review_payload = self._review_and_repair_narration(
            db=db,
            campaign=campaign,
            actor=actor,
            user_input=normalized_user_input,
            intent=intent,
            state=intent_state,
            context_window=llm_context,
            system_prompt=system_prompt,
            initial_narration=safe_narration,
            enable_llm_review=not self._should_bypass_llm_review(campaign, intent, runtime_constraints),
        )
        filtered_ai_commands, outcome_rejected = self._filter_commands_for_narrative_outcome(
            ai_response.commands, final_narration
        )
        llm_validation = validate_commands(intent_state, filtered_ai_commands)
        new_state = apply_commands(intent_state, llm_validation["accepted"])
        new_state = tick_effects(new_state)
        rejected = self._dedupe_rejections(
            [*intent_validation["rejected"], *llm_validation["rejected"], *outcome_rejected]
        )

        campaign.state = new_state.model_dump()
        self.sync_inventory_for_player(db, player, new_state, auto_commit=False)
        self.sync_character_row(db, campaign, player, new_state, auto_commit=False)

        turn = TurnLog(
            campaign_id=campaign.id,
            actor=actor,
            user_input=user_input,
            ai_raw_output=self.serialize_ai_raw_output(
                {
                    "source": "llm_turn",
                    "correlation_id": turn_correlation_id,
                    "intent": intent.model_dump(),
                    "intent_validation": {
                        "accepted": [c.model_dump() for c in intent_validation["accepted"]],
                        "rejected": intent_validation["rejected"],
                    },
                    "response": {**ai_response.model_dump(), "narration": final_narration},
                    "review": review_payload,
                    "state_before": state_before,
                    "state_after": new_state.model_dump(),
                }
            ),
            accepted_commands=[
                *[c.model_dump() for c in intent_validation["accepted"]],
                *[c.model_dump() for c in llm_validation["accepted"]],
            ],
            rejected_commands=rejected,
            narration=final_narration,
        )
        db.add(turn)
        db.flush()
        self._record_learned_relevance(
            db,
            scene=intent_state.scene,
            user_input=normalized_user_input,
            intent=intent,
            accepted_commands=[*intent_validation["accepted"], *llm_validation["accepted"]],
            turn_log_id=turn.id,
        )
        db.add(campaign)
        db.commit()
        self._refresh_long_term_memory(db, campaign)

        return final_narration, {
            "accepted": [
                *[c.model_dump() for c in intent_validation["accepted"]],
                *[c.model_dump() for c in llm_validation["accepted"]],
            ],
            "rejected": rejected,
            "context": context_window,
            "correlation_id": turn_correlation_id,
            "auto_created_default_character": auto_created_default_character,
            "auto_character_name": auto_character_name,
        }

    def turn_engine_for_campaign(self, db: Session, campaign: Campaign) -> str:
        rules = self.list_rules(db, campaign)
        engine = rules.get("turn_engine", "classic").strip().lower()
        return "crew" if engine == "crew" else "classic"

    def preview_turn_with_crew(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        user_input: str,
        crew_definition_json: str | None = None,
    ) -> dict:
        current_state = WorldState.model_validate(campaign.state)
        player_name = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor)
            .with_entities(Player.display_name)
            .scalar()
        )
        normalized_user_input = self._normalize_quoted_player_input(user_input, player_name)
        player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor)
            .one_or_none()
        )
        actor_char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id) if player else None
        context_window = self.context_builder.build(db, campaign, actor_id=actor)
        context_window["actor_inventory"] = self._actor_inventory_for_context(db, campaign, actor, current_state)
        context_window["actor_character_name"] = actor_char_name
        context_window["conversation_history"] = self._full_conversation_for_context(db, campaign)
        context_window["raw_user_input"] = user_input
        context_window["normalized_user_input"] = normalized_user_input
        system_prompt = self.build_campaign_system_prompt(db, campaign)
        system_prompt = self._augment_system_prompt_with_actor(system_prompt, actor_char_name)
        runtime_constraints: list[str] = []
        intent = PlayerIntentExtraction(inventory=[], commands=[])
        intent_validation = {"accepted": [], "rejected": []}
        if player:
            llm_context = self._packed_llm_context(
                db, campaign, context_window, current_state, actor_char_name, normalized_user_input
            )
            intent = self._extract_intent(normalized_user_input, current_state, llm_context, system_prompt)
            runtime_constraints = self._runtime_constraints_from_intent(
                db, campaign, player, current_state, intent, user_input=normalized_user_input
            )
            inventory_resolution = self._resolve_inventory_actions_from_intent(
                db, campaign, player, current_state, intent, user_input=normalized_user_input
            )
            if inventory_resolution and inventory_resolution["handled"]:
                return {
                    "narration": inventory_resolution["narration"],
                    "accepted": [c.model_dump() for c in inventory_resolution["accepted"]],
                    "rejected": [],
                    "context": {},
                    "crew_outputs": {},
                }
            intent_commands = [
                c
                for c in self._commands_from_intent(db, campaign, player, intent)
                if c.type not in {"add_item", "remove_item"}
            ]
            intent_validation = validate_commands(current_state, intent_commands)
            current_state = apply_commands(current_state, intent_validation["accepted"])
        if runtime_constraints:
            context_window["runtime_constraints"] = runtime_constraints
        context_window["intent"] = intent.model_dump()
        context_window["intent_validation"] = {
            "accepted": [c.model_dump() for c in intent_validation["accepted"]],
            "rejected": intent_validation["rejected"],
        }
        system_prompt = self._augment_system_prompt_with_runtime_constraints(system_prompt, runtime_constraints)
        llm_context = self._packed_llm_context(
            db, campaign, context_window, current_state, actor_char_name, normalized_user_input
        )
        crew_definition = self.crew.parse_definition(crew_definition_json)
        ai_response, crew_outputs = self.crew.run(
            user_input=normalized_user_input,
            state=current_state,
            mode=campaign.mode,
            context_json=json.dumps(llm_context),
            system_prompt=system_prompt,
            crew_definition=crew_definition,
        )
        safe_narration = self._enforce_other_player_agency_on_narration(db, campaign, actor, ai_response.narration)
        final_narration, review_payload = self._review_and_repair_narration(
            db=db,
            campaign=campaign,
            actor=actor,
            user_input=normalized_user_input,
            intent=intent,
            state=current_state,
            context_window=llm_context,
            system_prompt=system_prompt,
            initial_narration=safe_narration,
            enable_llm_review=not self._should_bypass_llm_review(campaign, intent, runtime_constraints),
        )
        validation = validate_commands(current_state, ai_response.commands)
        rejected = self._dedupe_rejections(intent_validation["rejected"] + validation["rejected"])
        return {
            "narration": final_narration,
            "accepted": [c.model_dump() for c in intent_validation["accepted"]]
            + [c.model_dump() for c in validation["accepted"]],
            "rejected": rejected,
            "context": {**context_window, "review": review_payload},
            "crew_outputs": crew_outputs,
        }

    def process_turn_with_crew(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        actor_display_name: str,
        user_input: str,
        crew_definition_json: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[str, dict]:
        turn_correlation_id = (correlation_id or "").strip() or self._new_turn_correlation_id()
        player = self.ensure_player(db, campaign, actor, actor_display_name)
        auto_character_name, auto_created_default_character = self.ensure_default_character_for_player(db, campaign, player)
        actor_char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        normalized_user_input = self._normalize_quoted_player_input(user_input, actor_display_name)
        current_state = WorldState.model_validate(campaign.state)
        state_before = current_state.model_dump()
        self_view = self._self_inspection_narration(current_state, actor_char_name, normalized_user_input)
        if self_view:
            turn = TurnLog(
                campaign_id=campaign.id,
                actor=actor,
                user_input=user_input,
                ai_raw_output=self.serialize_ai_raw_output(
                    {
                        "source": "deterministic_self_query",
                        "correlation_id": turn_correlation_id,
                        "actor_character_name": actor_char_name,
                        "normalized_user_input": normalized_user_input,
                    }
                ),
                accepted_commands=[],
                rejected_commands=[],
                narration=self_view,
            )
            db.add(turn)
            db.commit()
            return self_view, {
                "accepted": [],
                "rejected": [],
                "context": {"correlation_id": turn_correlation_id},
                "auto_created_default_character": auto_created_default_character,
                "auto_character_name": auto_character_name,
                "crew_outputs": {},
            }
        context_window = self.context_builder.build(db, campaign, actor_id=actor)
        context_window["correlation_id"] = turn_correlation_id
        context_window["actor_inventory"] = self._actor_inventory_for_context(db, campaign, actor, current_state)
        context_window["actor_character_name"] = actor_char_name
        context_window["conversation_history"] = self._full_conversation_for_context(db, campaign)
        context_window["raw_user_input"] = user_input
        context_window["normalized_user_input"] = normalized_user_input
        system_prompt = self.build_campaign_system_prompt(db, campaign)
        system_prompt = self._augment_system_prompt_with_actor(system_prompt, actor_char_name)
        llm_context = self._packed_llm_context(
            db, campaign, context_window, current_state, actor_char_name, normalized_user_input
        )
        intent = self._extract_intent(normalized_user_input, current_state, llm_context, system_prompt)
        runtime_constraints = self._runtime_constraints_from_intent(
            db, campaign, player, current_state, intent, user_input=normalized_user_input
        )

        inventory_resolution = self._resolve_inventory_actions_from_intent(
            db, campaign, player, current_state, intent, user_input=normalized_user_input
        )
        if inventory_resolution and inventory_resolution["handled"]:
            accepted_cmds: list[Command] = inventory_resolution["accepted"]
            new_state = apply_commands(current_state, accepted_cmds)
            new_state = tick_effects(new_state)
            campaign.state = new_state.model_dump()
            self.sync_inventory_for_player(db, player, new_state, auto_commit=False)
            self.sync_character_row(db, campaign, player, new_state, auto_commit=False)

            turn = TurnLog(
                campaign_id=campaign.id,
                actor=actor,
                user_input=user_input,
                ai_raw_output=self.serialize_ai_raw_output(
                    {
                        "source": "intent_inventory_resolver",
                        "correlation_id": turn_correlation_id,
                        "intent": intent.model_dump(),
                        "state_before": state_before,
                        "state_after": new_state.model_dump(),
                    }
                ),
                accepted_commands=[c.model_dump() for c in accepted_cmds],
                rejected_commands=[],
                narration=inventory_resolution["narration"],
            )
            db.add(turn)
            db.flush()
            self._record_learned_relevance(
                db,
                scene=current_state.scene,
                user_input=normalized_user_input,
                intent=intent,
                accepted_commands=accepted_cmds,
                turn_log_id=turn.id,
            )
            db.add(campaign)
            db.commit()
            self._refresh_long_term_memory(db, campaign)
            return inventory_resolution["narration"], {
                "accepted": [c.model_dump() for c in accepted_cmds],
                "rejected": [],
                "context": {"correlation_id": turn_correlation_id},
                "crew_outputs": {},
                "auto_created_default_character": auto_created_default_character,
                "auto_character_name": auto_character_name,
            }

        intent_commands = [
            c
            for c in self._commands_from_intent(db, campaign, player, intent)
            if c.type not in {"add_item", "remove_item"}
        ]
        intent_validation = validate_commands(current_state, intent_commands)
        intent_state = apply_commands(current_state, intent_validation["accepted"])

        if runtime_constraints:
            context_window["runtime_constraints"] = runtime_constraints
        context_window["intent"] = intent.model_dump()
        context_window["intent_validation"] = {
            "accepted": [c.model_dump() for c in intent_validation["accepted"]],
            "rejected": intent_validation["rejected"],
        }
        system_prompt = self._augment_system_prompt_with_runtime_constraints(system_prompt, runtime_constraints)
        llm_context = self._packed_llm_context(
            db, campaign, context_window, intent_state, actor_char_name, normalized_user_input
        )
        crew_definition = self.crew.parse_definition(crew_definition_json)
        ai_response, crew_outputs = self.crew.run(
            user_input=normalized_user_input,
            state=intent_state,
            mode=campaign.mode,
            context_json=json.dumps(llm_context),
            system_prompt=system_prompt,
            crew_definition=crew_definition,
        )

        safe_narration = self._enforce_other_player_agency_on_narration(db, campaign, actor, ai_response.narration)
        final_narration, review_payload = self._review_and_repair_narration(
            db=db,
            campaign=campaign,
            actor=actor,
            user_input=normalized_user_input,
            intent=intent,
            state=intent_state,
            context_window=llm_context,
            system_prompt=system_prompt,
            initial_narration=safe_narration,
            enable_llm_review=not self._should_bypass_llm_review(campaign, intent, runtime_constraints),
        )
        filtered_ai_commands, outcome_rejected = self._filter_commands_for_narrative_outcome(
            ai_response.commands, final_narration
        )
        llm_validation = validate_commands(intent_state, filtered_ai_commands)
        new_state = apply_commands(intent_state, llm_validation["accepted"])
        new_state = tick_effects(new_state)
        rejected = self._dedupe_rejections(
            [*intent_validation["rejected"], *llm_validation["rejected"], *outcome_rejected]
        )

        campaign.state = new_state.model_dump()
        self.sync_inventory_for_player(db, player, new_state, auto_commit=False)
        self.sync_character_row(db, campaign, player, new_state, auto_commit=False)

        turn = TurnLog(
            campaign_id=campaign.id,
            actor=actor,
            user_input=user_input,
            ai_raw_output=self.serialize_ai_raw_output(
                {
                    "source": "crew_turn",
                    "correlation_id": turn_correlation_id,
                    "intent": intent.model_dump(),
                    "intent_validation": {
                        "accepted": [c.model_dump() for c in intent_validation["accepted"]],
                        "rejected": intent_validation["rejected"],
                    },
                    "response": {**ai_response.model_dump(), "narration": final_narration},
                    "review": review_payload,
                    "crew_outputs": crew_outputs,
                    "state_before": state_before,
                    "state_after": new_state.model_dump(),
                }
            ),
            accepted_commands=[
                *[c.model_dump() for c in intent_validation["accepted"]],
                *[c.model_dump() for c in llm_validation["accepted"]],
            ],
            rejected_commands=rejected,
            narration=final_narration,
        )
        db.add(turn)
        db.flush()
        self._record_learned_relevance(
            db,
            scene=intent_state.scene,
            user_input=normalized_user_input,
            intent=intent,
            accepted_commands=[*intent_validation["accepted"], *llm_validation["accepted"]],
            turn_log_id=turn.id,
        )
        db.add(campaign)
        db.commit()
        self._refresh_long_term_memory(db, campaign)

        return final_narration, {
            "accepted": [
                *[c.model_dump() for c in intent_validation["accepted"]],
                *[c.model_dump() for c in llm_validation["accepted"]],
            ],
            "rejected": rejected,
            "context": context_window,
            "crew_outputs": crew_outputs,
            "correlation_id": turn_correlation_id,
            "auto_created_default_character": auto_created_default_character,
            "auto_character_name": auto_character_name,
        }

    def process_turn_routed(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        actor_display_name: str,
        user_input: str,
    ) -> tuple[str, dict]:
        turn_correlation_id = self._new_turn_correlation_id()
        try:
            rules = self.list_rules(db, campaign)
            if self.turn_engine_for_campaign(db, campaign) == "crew":
                return self.process_turn_with_crew(
                    db,
                    campaign=campaign,
                    actor=actor,
                    actor_display_name=actor_display_name,
                    user_input=user_input,
                    crew_definition_json=rules.get("agent_crew_definition"),
                    correlation_id=turn_correlation_id,
                )
            return self.process_turn(
                db,
                campaign=campaign,
                actor=actor,
                actor_display_name=actor_display_name,
                user_input=user_input,
                correlation_id=turn_correlation_id,
            )
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            rollback_payload = {
                "schema_version": 1,
                "source": "turn_rollback",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(limit=15),
                "correlation_id": turn_correlation_id,
                "actor": actor,
                "user_input": user_input,
            }
            narration = "Turn failed and was rolled back safely. Please retry your action."
            try:
                db.add(
                    TurnLog(
                        campaign_id=campaign.id,
                        actor=actor,
                        user_input=user_input,
                        ai_raw_output=self.serialize_ai_raw_output(rollback_payload),
                        accepted_commands=[],
                        rejected_commands=[{"reason": "turn_rolled_back", "error": str(exc)}],
                        narration=narration,
                    )
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            return narration, {
                "accepted": [],
                "rejected": [{"reason": "turn_rolled_back", "error": str(exc)}],
                "context": {"rollback_envelope": rollback_payload, "correlation_id": turn_correlation_id},
            }



