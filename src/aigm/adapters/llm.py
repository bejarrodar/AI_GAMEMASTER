from __future__ import annotations

import json
import re
import threading
import time
from difflib import get_close_matches
from urllib import error, request

from openai import OpenAI

from aigm.config import settings
from aigm.schemas.game import AIResponse, CharacterState, Command, OutputReview, PlayerIntentExtraction, WorldState


class LLMAdapter:
    VALID_COMMAND_TYPES = {
        "narrate",
        "set_scene",
        "adjust_hp",
        "add_item",
        "remove_item",
        "set_item_state",
        "set_flag",
        "set_stat",
        "add_effect",
        "remove_effect",
    }
    VALID_INTENT_ACTIONS = {"use", "add", "remove", "steal", "pickup", "other"}
    VALID_OWNERS = {"self", "target", "scene", "unknown"}
    VALID_PORTABILITY = {"portable", "non_portable", "unknown"}
    _circuit_lock = threading.Lock()
    _circuit_state: dict[str, dict[str, float | int]] = {
        "ollama": {"failures": 0, "opened_until": 0.0},
        "openai": {"failures": 0, "opened_until": 0.0},
    }

    @classmethod
    def _circuit_is_open(cls, provider: str) -> bool:
        p = provider.strip().lower()
        with cls._circuit_lock:
            state = cls._circuit_state.get(p, {"opened_until": 0.0})
            return float(state.get("opened_until", 0.0) or 0.0) > time.time()

    @classmethod
    def _record_provider_success(cls, provider: str) -> None:
        p = provider.strip().lower()
        with cls._circuit_lock:
            cls._circuit_state[p] = {"failures": 0, "opened_until": 0.0}

    @classmethod
    def _record_provider_failure(cls, provider: str) -> None:
        p = provider.strip().lower()
        threshold = max(1, int(settings.llm_circuit_breaker_failure_threshold))
        reset_s = max(1, int(settings.llm_circuit_breaker_reset_s))
        now = time.time()
        with cls._circuit_lock:
            state = cls._circuit_state.setdefault(p, {"failures": 0, "opened_until": 0.0})
            failures = int(state.get("failures", 0) or 0) + 1
            opened_until = float(state.get("opened_until", 0.0) or 0.0)
            if failures >= threshold:
                opened_until = now + float(reset_s)
                failures = 0
            cls._circuit_state[p] = {"failures": failures, "opened_until": opened_until}

    @staticmethod
    def _urlopen_json_with_retry(req: request.Request, timeout_s: int | float) -> dict:
        last_exc: Exception | None = None
        attempts = max(0, int(settings.llm_http_max_retries)) + 1
        backoff = max(0.0, float(settings.llm_http_retry_backoff_s))
        for attempt in range(attempts):
            try:
                with request.urlopen(req, timeout=timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except error.HTTPError as exc:
                last_exc = exc
                # Retry transient classes only.
                if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504} or attempt >= attempts - 1:
                    raise
            except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt >= attempts - 1:
                    raise
            if backoff > 0:
                time.sleep(backoff * (2**attempt))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("LLM HTTP request failed without exception details.")

    @staticmethod
    def _model_for_task(task: str) -> str:
        provider = settings.llm_provider.strip().lower()
        if provider == "openai":
            if task == "narration":
                return settings.openai_model_narration.strip() or settings.openai_model
            if task == "intent":
                return settings.openai_model_intent.strip() or settings.openai_model
            if task == "review":
                return settings.openai_model_review.strip() or settings.openai_model
            return settings.openai_model
        if task == "narration":
            return settings.ollama_model_narration.strip() or settings.ollama_model
        if task == "intent":
            return settings.ollama_model_intent.strip() or settings.ollama_model
        if task == "review":
            return settings.ollama_model_review.strip() or settings.ollama_model
        return settings.ollama_model

    @staticmethod
    def _options_for_task(task: str) -> dict:
        if task in {"intent", "review"}:
            return {
                "temperature": settings.ollama_json_temperature,
                "num_predict": settings.ollama_json_num_predict,
            }
        return {
            "temperature": settings.ollama_gen_temperature,
            "num_predict": settings.ollama_gen_num_predict,
        }

    @staticmethod
    def _openai_temperature_for_task(task: str) -> float:
        if task in {"intent", "review"}:
            return settings.ollama_json_temperature
        return settings.ollama_gen_temperature

    @staticmethod
    def _openai_client() -> OpenAI:
        kwargs: dict = {}
        if settings.openai_api_key.strip():
            kwargs["api_key"] = settings.openai_api_key.strip()
        if settings.openai_base_url.strip():
            kwargs["base_url"] = settings.openai_base_url.strip()
        return OpenAI(**kwargs)

    @classmethod
    def _chat_json_with_openai(cls, *, task: str, system_prompt: str, user_prompt: str) -> dict:
        client = cls._openai_client()
        payload: dict = {
            "model": cls._model_for_task(task),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": cls._openai_temperature_for_task(task),
            "timeout": settings.openai_timeout_s,
        }
        if settings.llm_json_mode_strict:
            payload["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**payload)
        content = response.choices[0].message.content if response.choices else ""
        if not content:
            return {}
        return cls._extract_json_object(content)

    @staticmethod
    def _contains_non_english_script(text: str) -> bool:
        return bool(
            re.search(
                r"[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]",
                text or "",
            )
        )

    @staticmethod
    def _classify_object(item_key: str) -> tuple[str, str]:
        text = item_key.strip().lower().replace("_", " ")
        if any(
            k in text
            for k in (
                "ruin",
                "building",
                "house",
                "tower",
                "castle",
                "fort",
                "wall",
                "bridge",
                "shop",
                "store",
                "storefront",
                "tavern",
                "inn",
                "temple",
                "tree",
                "oak",
                "pine",
                "wagon",
                "cart",
                "carriage",
                "boulder",
            )
        ):
            return "structure", "non_portable"
        if any(k in text for k in ("mountain", "hill", "forest", "river", "lake", "ocean")):
            return "terrain", "non_portable"
        if any(k in text for k in ("stick", "dagger", "sword", "potion", "coin", "scroll", "key", "ring", "torch")):
            return "small_object", "portable"
        return "unknown", "unknown"

    @classmethod
    def _default_feasibility_for_inventory_intent(cls, action: str, item_key: str, target_character: str | None) -> dict:
        object_type, portability = cls._classify_object(item_key)
        if action in {"pickup", "add"} and portability == "non_portable":
            return {
                "action": action,
                "item_key": item_key,
                "target_character": target_character,
                "question": f"Can '{item_key}' be placed in inventory?",
                "is_possible": False,
                "reason": f"'{item_key}' is a {object_type} and is not portable.",
                "object_type": object_type,
                "portability": portability,
            }
        return {
            "action": action,
            "item_key": item_key,
            "target_character": target_character,
            "question": f"Is '{item_key}' usable for action '{action}' right now?",
            "is_possible": True if portability != "unknown" else False,
            "reason": "Portable item by classification." if portability == "portable" else "Insufficient certainty; requires context.",
            "object_type": object_type,
            "portability": portability,
        }

    @staticmethod
    def _normalize_candidate_name(raw: str) -> str:
        cleaned = raw.strip().strip("\"'.,:;!?()[]{}")
        parts = [p for p in re.split(r"\s+", cleaned) if p]
        if not parts:
            return ""
        parts = parts[:2]
        normalized = " ".join(p.strip("-'") for p in parts if p.strip("-'"))
        return normalized

    @classmethod
    def _extract_character_name(cls, description: str) -> str | None:
        # Handle stylized spell-out patterns like "It's O-S-C-A-R ... It's M-A-Y-E-R."
        spelled = re.findall(r"\bit[’']?s\s+([A-Za-z](?:-[A-Za-z]){2,})\b", description, flags=re.IGNORECASE)
        if len(spelled) >= 2:
            first = spelled[0].replace("-", "").strip()
            second = spelled[1].replace("-", "").strip()
            combined = cls._normalize_candidate_name(f"{first} {second}")
            if combined:
                return combined
        if len(spelled) == 1:
            single = cls._normalize_candidate_name(spelled[0].replace("-", ""))
            if single:
                return single

        patterns = [
            r"\bmy\s+name\s+is\s+([A-Za-z][A-Za-z\-']+(?:\s+[A-Za-z][A-Za-z\-']+)?)\b",
            r"\bname\s*[:=]\s*([A-Za-z][A-Za-z\-']+(?:\s+[A-Za-z][A-Za-z\-']+)?)\b",
            r"\bi\s+am\s+([A-Za-z][A-Za-z\-']+(?:\s+[A-Za-z][A-Za-z\-']+)?)\b",
            r"\bi'm\s+([A-Za-z][A-Za-z\-']+(?:\s+[A-Za-z][A-Za-z\-']+)?)\b",
            r"\bnamed\s+([A-Za-z][A-Za-z\-']+(?:\s+[A-Za-z][A-Za-z\-']+)?)\b",
            r"\bcalled\s+([A-Za-z][A-Za-z\-']+(?:\s+[A-Za-z][A-Za-z\-']+)?)\b",
        ]
        lower_desc = description.lower()
        if "i am a " in lower_desc or "i'm a " in lower_desc:
            patterns = [p for p in patterns if "i\\s+am\\s+" not in p and "i'm\\s+" not in p] + patterns
        for pattern in patterns:
            match = re.search(pattern, description, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = cls._normalize_candidate_name(match.group(1))
            if candidate:
                return candidate
        return None

    @classmethod
    def _default_starter_inventory(cls, description: str) -> dict[str, int]:
        lower_desc = description.lower()
        if any(k in lower_desc for k in ("mage", "wizard", "sorcer", "warlock")):
            return {"arcane_focus": 1, "spellbook": 1, "travel_cloak": 1}
        if any(k in lower_desc for k in ("druid", "ranger")):
            return {"wooden_staff": 1, "herb_pouch": 1, "traveler_pack": 1}
        if any(k in lower_desc for k in ("rogue", "thief", "assassin")):
            return {"dagger": 1, "lockpick_set": 1, "dark_cloak": 1}
        if any(k in lower_desc for k in ("fighter", "knight", "barbarian", "burly")):
            return {"weapon": 1, "shield": 1, "rations": 1}
        return {"bedroll": 1, "waterskin": 1, "rations": 1}

    @classmethod
    def _coerce_character_profile(cls, parsed: dict, description: str, fallback_name: str) -> CharacterState:
        inferred_name = str(parsed.get("name", "") or "").strip()
        parsed_name = cls._extract_character_name(description)
        name = inferred_name or parsed_name or fallback_name

        hp_raw = parsed.get("hp", 10)
        try:
            hp = int(hp_raw)
        except (TypeError, ValueError):
            hp = 10
        hp = max(1, min(30, hp))

        stats_raw = parsed.get("stats", {}) if isinstance(parsed.get("stats"), dict) else {}
        stats: dict[str, int] = {}
        for key in ("str", "dex", "int"):
            value = stats_raw.get(key, 10)
            try:
                stats[key] = max(3, min(20, int(value)))
            except (TypeError, ValueError):
                stats[key] = 10

        # Preserve exact user-provided description as canonical character text.
        desc = description.strip()

        inventory: dict[str, int] = {}
        inv_raw = parsed.get("inventory", [])
        if isinstance(inv_raw, dict):
            for item_key, qty in inv_raw.items():
                key = re.sub(r"\s+", "_", str(item_key or "").strip().lower())
                if not key:
                    continue
                try:
                    quantity = int(qty)
                except (TypeError, ValueError):
                    quantity = 1
                if quantity > 0:
                    inventory[key] = quantity
        elif isinstance(inv_raw, list):
            for row in inv_raw:
                if not isinstance(row, dict):
                    continue
                key = re.sub(r"\s+", "_", str(row.get("item_key", "") or "").strip().lower())
                if not key:
                    continue
                try:
                    quantity = int(row.get("quantity", 1))
                except (TypeError, ValueError):
                    quantity = 1
                if quantity > 0:
                    inventory[key] = inventory.get(key, 0) + quantity

        if not inventory:
            inventory = cls._default_starter_inventory(description)
        elif "stick" in description.lower() and "stick" not in inventory:
            inventory["stick"] = 1

        effects = []
        effects_raw = parsed.get("effects", []) if isinstance(parsed.get("effects"), list) else []
        for row in effects_raw:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key", "") or "").strip().lower().replace(" ", "_")
            if not key:
                continue
            category = str(row.get("category", "misc") or "misc").strip().lower()
            if category not in {"magical", "physical", "misc"}:
                category = "misc"
            description_text = str(row.get("description", "") or "").strip()
            duration = row.get("duration_turns")
            try:
                duration_int = None if duration in {None, ""} else int(duration)
            except (TypeError, ValueError):
                duration_int = None
            if duration_int is not None and duration_int < 1:
                duration_int = None
            effects.append(
                {
                    "key": key,
                    "category": category,
                    "description": description_text,
                    "duration_turns": duration_int,
                }
            )

        return CharacterState.model_validate(
            {
                "name": name,
                "description": desc,
                "hp": hp,
                "max_hp": hp,
                "stats": stats,
                "inventory": inventory,
                "item_states": {},
                "effects": effects,
            }
        )

    def _character_profile_with_ollama(self, description: str, fallback_name: str) -> CharacterState:
        json_shape = (
            "Return JSON only with this shape: "
            '{"name":"string","description":"string","hp":10,"stats":{"str":10,"dex":10,"int":10},'
            '"inventory":[{"item_key":"string","quantity":1}],"effects":[{"key":"string","category":"magical|physical|misc",'
            '"description":"string","duration_turns":null}]}.'
        )
        prompt = (
            f"{json_shape}\n"
            "Extract a player character profile from the description.\n"
            "Use English. Infer the intended character name if present.\n"
            "If explicit starting gear is not provided, generate 2-4 starter equipment items that fit the described archetype.\n"
            f"fallback_name={fallback_name}\n"
            f"description={description}"
        )
        payload = {
            "model": self._model_for_task("intent"),
            "messages": [
                {"role": "system", "content": "You extract structured RPG character data."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("intent"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=settings.ollama_timeout_s)
        content = body.get("message", {}).get("content", "")
        parsed = self._extract_json_object(content) if content else {}
        return self._coerce_character_profile(parsed, description, fallback_name)

    def _character_profile_with_openai(self, description: str, fallback_name: str) -> CharacterState:
        json_shape = (
            "Return JSON only with this shape: "
            '{"name":"string","description":"string","hp":10,"stats":{"str":10,"dex":10,"int":10},'
            '"inventory":[{"item_key":"string","quantity":1}],"effects":[{"key":"string","category":"magical|physical|misc",'
            '"description":"string","duration_turns":null}]}.'
        )
        prompt = (
            f"{json_shape}\n"
            "Extract a player character profile from the description.\n"
            "Use English. Infer the intended character name if present.\n"
            "If explicit starting gear is not provided, generate 2-4 starter equipment items that fit the described archetype.\n"
            f"fallback_name={fallback_name}\n"
            f"description={description}"
        )
        parsed = self._chat_json_with_openai(
            task="intent",
            system_prompt="You extract structured RPG character data.",
            user_prompt=prompt,
        )
        return self._coerce_character_profile(parsed, description, fallback_name)

    @staticmethod
    def _fallback_narration(user_input: str, state_json: str, mode: str) -> str:
        scene = "The situation hangs in tense silence."
        try:
            parsed_state = json.loads(state_json)
            parsed_scene = str(parsed_state.get("scene", "")).strip()
            if parsed_scene:
                scene = parsed_scene
        except json.JSONDecodeError:
            pass

        tone = "cinematic and dangerous" if mode == "dnd" else "atmospheric and character-driven"
        return (
            f"{scene}\n\n"
            f"You move with intent: {user_input}. The moment shifts in a {tone} way as nearby details react "
            f"to your choice. What do you do next?"
        )

    def _fallback_response(self, user_input: str, state_json: str, mode: str) -> AIResponse:
        fallback = {
            "narration": self._fallback_narration(user_input, state_json, mode),
            "commands": [{"type": "narrate", "text": user_input}],
        }
        return AIResponse.model_validate(json.loads(json.dumps(fallback)))

    @staticmethod
    def _sanitize_json_candidate(raw: str) -> str:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        text = text.replace("“", '"').replace("”", '"').replace("’", "'")
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        return text

    @classmethod
    def _extract_json_object(cls, raw: str) -> dict:
        cleaned = cls._sanitize_json_candidate(raw)
        first_err: json.JSONDecodeError | None = None
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            first_err = exc

        # Extract first balanced JSON object from mixed model text.
        n = len(cleaned)
        for i, ch in enumerate(cleaned):
            if ch != "{":
                continue
            depth = 0
            in_str = False
            escaped = False
            for j in range(i, n):
                c = cleaned[j]
                if in_str:
                    if escaped:
                        escaped = False
                    elif c == "\\":
                        escaped = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = cls._sanitize_json_candidate(cleaned[i : j + 1])
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
            # continue searching next '{'

        # Final best-effort greedy fallback for legacy behavior.
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            candidate = cls._sanitize_json_candidate(match.group(0))
            return json.loads(candidate)
        if first_err is not None:
            raise first_err
        raise json.JSONDecodeError("No JSON object found", cleaned, 0)

    @classmethod
    def _coerce_ai_response(cls, parsed: dict) -> AIResponse:
        narration = str(parsed.get("narration", "")).strip() or "The scene shifts."
        commands_raw = parsed.get("commands", [])
        if not isinstance(commands_raw, list):
            commands_raw = []

        coerced_commands: list[Command] = []
        for row in commands_raw:
            if not isinstance(row, dict):
                continue
            row_type = str(row.get("type", "")).strip().lower()

            # Common non-schema output from smaller models; preserve intent as narration/no-op.
            if row_type in {"command", "action"}:
                row_type = "narrate"

            if row_type not in cls.VALID_COMMAND_TYPES:
                continue

            candidate = dict(row)
            candidate["type"] = row_type
            try:
                coerced_commands.append(Command.model_validate(candidate))
            except Exception:
                continue

        return AIResponse(narration=narration, commands=coerced_commands)

    @classmethod
    def _coerce_player_intent(cls, parsed: dict) -> PlayerIntentExtraction:
        inventory_raw = parsed.get("inventory", [])
        commands_raw = parsed.get("commands", [])
        checks_raw = parsed.get("feasibility_checks", [])
        relevance_raw = parsed.get("relevance_signals", [])

        if not isinstance(inventory_raw, list):
            inventory_raw = []
        if not isinstance(commands_raw, list):
            commands_raw = []
        if not isinstance(checks_raw, list):
            checks_raw = []
        if not isinstance(relevance_raw, list):
            relevance_raw = []

        inventory: list[dict] = []
        for row in inventory_raw:
            if not isinstance(row, dict):
                continue
            action = str(row.get("action", "other")).strip().lower()
            if action not in cls.VALID_INTENT_ACTIONS:
                action = "other"
            item_key = row.get("item_key")
            if item_key is None:
                item_key = row.get("key")
            if item_key is None:
                item_key = row.get("text")
            item_key = str(item_key or "").strip()
            quantity = row.get("quantity", 1)
            if not isinstance(quantity, int):
                try:
                    quantity = int(quantity)
                except (TypeError, ValueError):
                    quantity = 1
            quantity = max(1, quantity)
            owner = str(row.get("owner", "unknown")).strip().lower()
            if owner not in cls.VALID_OWNERS:
                owner = "unknown"
            target_character = row.get("target_character")
            target_character = str(target_character).strip() if target_character not in (None, "") else None

            if not item_key and action != "other":
                continue
            inventory.append(
                {
                    "action": action,
                    "item_key": item_key,
                    "quantity": quantity,
                    "target_character": target_character,
                    "owner": owner,
                }
            )

        commands: list[dict] = []
        for row in commands_raw:
            if not isinstance(row, dict):
                continue
            row_type = str(row.get("type", "")).strip().lower()
            if row_type in {"command", "action"}:
                row_type = "narrate"
            if row_type not in cls.VALID_COMMAND_TYPES:
                continue
            candidate = dict(row)
            candidate["type"] = row_type
            try:
                commands.append(Command.model_validate(candidate).model_dump())
            except Exception:
                continue

        feasibility_checks: list[dict] = []
        for row in checks_raw:
            if not isinstance(row, dict):
                continue
            action = str(row.get("action", "other")).strip().lower()
            if action not in cls.VALID_INTENT_ACTIONS:
                action = "other"
            item_key = str(row.get("item_key", "") or "").strip()
            target_character = row.get("target_character")
            target_character = str(target_character).strip() if target_character not in (None, "") else None
            question = str(row.get("question", "") or "").strip()
            if not question:
                question = f"Is '{item_key or 'this action'}' possible?"
            is_possible = row.get("is_possible", True)
            if not isinstance(is_possible, bool):
                is_possible = str(is_possible).strip().lower() in {"1", "true", "yes", "y"}
            reason = str(row.get("reason", "") or "").strip()
            object_type = str(row.get("object_type", "unknown") or "unknown").strip().lower()
            portability = str(row.get("portability", "unknown") or "unknown").strip().lower()
            if portability not in cls.VALID_PORTABILITY:
                portability = "unknown"

            feasibility_checks.append(
                {
                    "action": action,
                    "item_key": item_key,
                    "target_character": target_character,
                    "question": question,
                    "is_possible": is_possible,
                    "reason": reason,
                    "object_type": object_type,
                    "portability": portability,
                    "requires_payment": (
                        row.get("requires_payment")
                        if isinstance(row.get("requires_payment"), bool) or row.get("requires_payment") is None
                        else str(row.get("requires_payment", "")).strip().lower() in {"1", "true", "yes", "y"}
                    ),
                    "cost_amount": (
                        int(row.get("cost_amount"))
                        if row.get("cost_amount") not in (None, "", "null")
                        and str(row.get("cost_amount", "")).strip().lstrip("-").isdigit()
                        else None
                    ),
                    "currency": (
                        str(row.get("currency", "")).strip().lower() if str(row.get("currency", "")).strip() else None
                    ),
                    "payer_owner": (
                        str(row.get("payer_owner", "")).strip().lower()
                        if str(row.get("payer_owner", "")).strip().lower() in cls.VALID_OWNERS
                        else None
                    ),
                    "has_required_funds": (
                        row.get("has_required_funds")
                        if isinstance(row.get("has_required_funds"), bool) or row.get("has_required_funds") is None
                        else str(row.get("has_required_funds", "")).strip().lower() in {"1", "true", "yes", "y"}
                    ),
                    "acquisition_mode": (
                        str(row.get("acquisition_mode", "")).strip().lower()
                        if str(row.get("acquisition_mode", "")).strip().lower()
                        in {"pickup", "purchase", "steal", "loot", "gift", "craft", "unknown"}
                        else None
                    ),
                    "would_be_theft": (
                        row.get("would_be_theft")
                        if isinstance(row.get("would_be_theft"), bool) or row.get("would_be_theft") is None
                        else str(row.get("would_be_theft", "")).strip().lower() in {"1", "true", "yes", "y"}
                    ),
                    "location_context": (
                        str(row.get("location_context", "")).strip() if str(row.get("location_context", "")).strip() else None
                    ),
                }
            )

        relevance_signals: list[dict] = []
        for row in relevance_raw:
            if not isinstance(row, dict):
                continue
            entity_type = str(row.get("entity_type", "other")).strip().lower()
            if entity_type not in {"item", "effect", "scene", "other"}:
                entity_type = "other"
            key = str(row.get("key", "") or "").strip()
            context_tag = str(row.get("context_tag", "general") or "general").strip().lower()
            if not context_tag:
                context_tag = "general"
            score_raw = row.get("score", 0.5)
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                score = 0.5
            score = max(0.0, min(1.0, score))
            reason = str(row.get("reason", "") or "").strip()
            relevance_signals.append(
                {
                    "entity_type": entity_type,
                    "key": key,
                    "context_tag": context_tag,
                    "score": score,
                    "reason": reason,
                }
            )

        return PlayerIntentExtraction.model_validate(
            {
                "inventory": inventory,
                "commands": commands,
                "feasibility_checks": feasibility_checks,
                "relevance_signals": relevance_signals,
            }
        )

    def _generate_with_ollama(
        self, user_input: str, state_json: str, mode: str, context_json: str, system_prompt: str
    ) -> AIResponse:
        json_shape = (
            "Return JSON only with this shape: "
            '{"narration":"string","commands":[{"type":"...","target":null,"key":null,"value":null,"amount":null,'
            '"text":null,"effect_category":null,"duration_turns":null}]}'
        )
        prompt = (
            f"{json_shape}\n\n"
            f"mode={mode}\n"
            f"context={context_json}\n"
            f"state={state_json}\n"
            f"user_input={user_input}"
        )
        payload = {
            "model": self._model_for_task("narration"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("narration"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=settings.ollama_timeout_s)
        content = body.get("message", {}).get("content", "")
        if content:
            parsed = self._extract_json_object(content)
            return self._coerce_ai_response(parsed)

        retry_payload = {
            "model": self._model_for_task("narration"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{json_shape}\n\n{prompt}"},
            ],
            "stream": False,
            "options": self._options_for_task("narration"),
        }
        if settings.llm_json_mode_strict:
            retry_payload["format"] = "json"
        retry_req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(retry_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        retry_body = self._urlopen_json_with_retry(retry_req, timeout_s=settings.ollama_timeout_s)
        retry_content = retry_body.get("message", {}).get("content", "")
        parsed_retry = self._extract_json_object(retry_content)
        return self._coerce_ai_response(parsed_retry)

    def _generate_with_openai(
        self, user_input: str, state_json: str, mode: str, context_json: str, system_prompt: str
    ) -> AIResponse:
        json_shape = (
            "Return JSON only with this shape: "
            '{"narration":"string","commands":[{"type":"...","target":null,"key":null,"value":null,"amount":null,'
            '"text":null,"effect_category":null,"duration_turns":null}]}'
        )
        prompt = (
            f"{json_shape}\n\n"
            f"mode={mode}\n"
            f"context={context_json}\n"
            f"state={state_json}\n"
            f"user_input={user_input}"
        )
        parsed = self._chat_json_with_openai(task="narration", system_prompt=system_prompt, user_prompt=prompt)
        return self._coerce_ai_response(parsed)

    def generate(self, user_input: str, state_json: str, mode: str, context_json: str, system_prompt: str) -> AIResponse:
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                return self._fallback_response(user_input, state_json, mode)
            try:
                out = self._generate_with_ollama(user_input, state_json, mode, context_json, system_prompt)
                self._record_provider_success("ollama")
                return out
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] Ollama failed, using fallback: {type(exc).__name__}: {exc}")
                return self._fallback_response(user_input, state_json, mode)
        if provider == "openai":
            if self._circuit_is_open("openai"):
                return self._fallback_response(user_input, state_json, mode)
            try:
                out = self._generate_with_openai(user_input, state_json, mode, context_json, system_prompt)
                self._record_provider_success("openai")
                return out
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
                return self._fallback_response(user_input, state_json, mode)
        _ = (state_json, mode, context_json, system_prompt)
        return self._fallback_response(user_input, state_json, mode)

    @classmethod
    def _fallback_extract_player_intent(cls, user_input: str, state_json: str = "") -> PlayerIntentExtraction:
        text = user_input.lower()
        intents: list[dict] = []
        commands: list[dict] = []
        feasibility_checks: list[dict] = []

        add_match = re.search(r"\b(?:put|place|stash|store|add)\s+(.+?)\s+into\s+my\s+inventory\b", text)
        if add_match:
            payload = add_match.group(1)
            for part in re.split(r"\s+and\s+|,\s*", payload):
                p = part.strip()
                if not p:
                    continue
                qty = 1
                m = re.match(r"^(?:the\s+)?(\d+)\s+(.+)$", p)
                if m:
                    qty = int(m.group(1))
                    p = m.group(2).strip()
                key = re.sub(r"[^a-z0-9\s_\-']", "", p)
                key = re.sub(r"^(?:my|the|a|an)\s+", "", key).strip()
                key = re.sub(r"\s+", "_", key)
                if key:
                    qty_norm = max(1, qty)
                    intents.append(
                        {
                            "action": "add",
                            "item_key": key,
                            "quantity": qty_norm,
                            "target_character": None,
                            "owner": "self",
                        }
                    )
                    commands.append({"type": "add_item", "key": key, "amount": qty_norm, "target": None})

        pull_match = re.search(
            r"\b(?:pull|draw|take|get|grab|use|ready|equip)\s+(?:a|an|the|my)?\s*([a-z][a-z0-9_\-']*)\s+"
            r"(?:out\s+of|from)\s+my\s+inventory\b",
            text,
        )
        if pull_match:
            item_key = pull_match.group(1).strip().lower()
            intents.append(
                {
                    "action": "use",
                    "item_key": item_key,
                    "quantity": 1,
                    "target_character": None,
                    "owner": "self",
                }
            )
            feasibility_checks.append(
                {
                    "action": "use",
                    "item_key": item_key,
                    "target_character": None,
                    "question": f"Can the player use '{item_key}' from inventory right now?",
                    "is_possible": True,
                    "reason": "Requires inventory verification against current state.",
                    "object_type": cls._classify_object(item_key)[0],
                    "portability": cls._classify_object(item_key)[1],
                }
            )

        steal_match = re.search(
            r"\b(?:steal|take|grab)\s+(?:a|an|the)?\s*([a-z][a-z0-9_\-']*)\s+from\s+([a-z][a-z0-9_\-']*)\b",
            text,
        )
        if steal_match:
            item_key = steal_match.group(1).strip().lower()
            target_name = steal_match.group(2).strip()
            intents.append(
                {
                    "action": "steal",
                    "item_key": item_key,
                    "quantity": 1,
                    "target_character": target_name,
                    "owner": "target",
                }
            )
            commands.append({"type": "remove_item", "target": target_name, "key": item_key, "amount": 1})
            feasibility_checks.append(
                {
                    "action": "steal",
                    "item_key": item_key,
                    "target_character": target_name,
                    "question": f"Can the player steal '{item_key}' from {target_name} right now?",
                    "is_possible": True,
                    "reason": "Requires target inventory verification against current state.",
                    "object_type": cls._classify_object(item_key)[0],
                    "portability": cls._classify_object(item_key)[1],
                }
            )

        pickup_match = re.search(r"\b(?:pick\s+up|pickup|grab|take)\s+(?:a|an|the)?\s*([a-z][a-z0-9_\-']*)\b", text)
        if pickup_match:
            item_key = pickup_match.group(1).strip().lower()
            intents.append(
                {
                    "action": "pickup",
                    "item_key": item_key,
                    "quantity": 1,
                    "target_character": None,
                    "owner": "scene",
                }
            )
            commands.append({"type": "add_item", "key": item_key, "amount": 1, "target": None})
            feasibility_checks.append(
                {
                    "action": "pickup",
                    "item_key": item_key,
                    "target_character": None,
                    "question": f"Can the player find '{item_key}' in the current scene?",
                    "is_possible": False,
                    "reason": "Requires explicit feasibility assessment from model/context.",
                    "object_type": "unknown",
                    "portability": "unknown",
                }
            )

        if not intents and not commands and not feasibility_checks and user_input.strip():
            commands.append(
                {
                    "type": "narrate",
                    "text": user_input.strip(),
                    "target": None,
                    "key": None,
                    "value": None,
                    "amount": None,
                    "effect_category": None,
                    "duration_turns": None,
                }
            )
            feasibility_checks.append(
                {
                    "action": "other",
                    "item_key": "",
                    "target_character": None,
                    "question": "Can this declared action be narrated without direct state mutation?",
                    "is_possible": True,
                    "reason": "No explicit structured mutation detected; narrative continuation is possible.",
                    "object_type": "action",
                    "portability": "unknown",
                }
            )

        return PlayerIntentExtraction.model_validate(
            {"inventory": intents, "commands": commands, "feasibility_checks": feasibility_checks}
        )

    @classmethod
    def _enrich_intent_from_text(
        cls, user_input: str, extracted: PlayerIntentExtraction, *, emergency_fallback: bool = True
    ) -> PlayerIntentExtraction:
        text = user_input.lower()
        inventory = list(extracted.inventory)
        commands = list(extracted.commands)
        feasibility_checks = list(extracted.feasibility_checks)

        def has_similar_inventory_intent(item_key: str, action: str) -> bool:
            for row in inventory:
                if row.item_key.lower() == item_key and row.action == action:
                    return True
            return False

        # Only allow regex-based intent inference during emergency fallback when model extraction failed.
        if not emergency_fallback:
            return PlayerIntentExtraction.model_validate(
                {
                    "inventory": [row.model_dump() if hasattr(row, "model_dump") else row for row in inventory],
                    "commands": [row.model_dump() if hasattr(row, "model_dump") else row for row in commands],
                    "feasibility_checks": [
                        row.model_dump() if hasattr(row, "model_dump") else row for row in feasibility_checks
                    ],
                }
            )

        # Capture attempts like "put player Bear ... into inventory" when model missed it.
        m = re.search(
            r"\b(?:put|place|shove|stuff|push)\s+(?:player\s+)?([a-z][a-z0-9_\-']*)\s+.*\b(?:into|inside)\b.+\binventory\b",
            text,
            flags=re.IGNORECASE,
        )
        if not m and re.search(r"\b(?:shove|stuff|push|put|place)\b.+\b(?:into|inside)\b.+\binventory\b", text):
            named_player = re.search(r"\bplayer\s+([a-z][a-z0-9_\-']*)\b", text, flags=re.IGNORECASE)
            if named_player:
                m = named_player
        if m:
            target = m.group(1).strip()
            if target in {"him", "her", "them"}:
                named_player = re.search(r"\bplayer\s+([a-z][a-z0-9_\-']*)\b", text, flags=re.IGNORECASE)
                if named_player:
                    target = named_player.group(1).strip()
            if target and not has_similar_inventory_intent(target, "add"):
                inventory.append(
                    {
                        "action": "add",
                        "item_key": target,
                        "quantity": 1,
                        "target_character": target,
                        "owner": "target",
                    }
                )
                feasibility_checks.append(
                    {
                        "action": "add",
                        "item_key": target,
                        "target_character": target,
                        "question": f"Can player character '{target}' be put into an inventory?",
                        "is_possible": False,
                        "reason": "Player characters are not valid inventory items.",
                        "object_type": "player_character",
                        "portability": "non_portable",
                    }
                )

        # Capture "pull <item> out of my inventory" as a use/remove attempt if missing.
        pull = re.search(
            r"\b(?:pull|draw|take|get|grab|use|ready|equip)\s+(?:a|an|the|my)?\s*([a-z][a-z0-9_\-']*)\s+"
            r"(?:out\s+of|from)\s+my\s+inventory\b",
            text,
            flags=re.IGNORECASE,
        )
        if pull:
            item = pull.group(1).strip().lower()
            if item and not has_similar_inventory_intent(item, "use"):
                inventory.append(
                    {"action": "use", "item_key": item, "quantity": 1, "target_character": None, "owner": "self"}
                )

        # Ensure each inventory intent has feasibility + type/portability metadata in emergency mode.
        for row in inventory:
            action = row.action if hasattr(row, "action") else row.get("action", "other")
            item_key = row.item_key if hasattr(row, "item_key") else row.get("item_key", "")
            target_character = (
                row.target_character if hasattr(row, "target_character") else row.get("target_character", None)
            )
            if not item_key:
                continue
            deterministic_object_type, deterministic_portability = cls._classify_object(item_key)
            exists = False
            for chk in feasibility_checks:
                chk_action = chk.action if hasattr(chk, "action") else chk.get("action", "")
                chk_item = chk.item_key if hasattr(chk, "item_key") else chk.get("item_key", "")
                if chk_action == action and chk_item == item_key:
                    # Emergency deterministic override for clearly non-portable items.
                    if action in {"pickup", "add"} and deterministic_portability == "non_portable":
                        if hasattr(chk, "is_possible"):
                            chk.is_possible = False
                            chk.reason = f"'{item_key}' is a {deterministic_object_type} and is not portable."
                            chk.object_type = deterministic_object_type
                            chk.portability = deterministic_portability
                        else:
                            chk["is_possible"] = False
                            chk["reason"] = f"'{item_key}' is a {deterministic_object_type} and is not portable."
                            chk["object_type"] = deterministic_object_type
                            chk["portability"] = deterministic_portability
                    exists = True
                    break
            if not exists:
                feasibility_checks.append(cls._default_feasibility_for_inventory_intent(action, item_key, target_character))

        if not inventory and not commands and not feasibility_checks and user_input.strip():
            commands.append(
                {
                    "type": "narrate",
                    "text": user_input.strip(),
                    "target": None,
                    "key": None,
                    "value": None,
                    "amount": None,
                    "effect_category": None,
                    "duration_turns": None,
                }
            )
            feasibility_checks.append(
                {
                    "action": "other",
                    "item_key": "",
                    "target_character": None,
                    "question": "Can this declared action be narrated without direct state mutation?",
                    "is_possible": True,
                    "reason": "No explicit structured mutation detected; narrative continuation is possible.",
                    "object_type": "action",
                    "portability": "unknown",
                }
            )

        return PlayerIntentExtraction.model_validate(
            {
                "inventory": [row.model_dump() if hasattr(row, "model_dump") else row for row in inventory],
                "commands": [row.model_dump() if hasattr(row, "model_dump") else row for row in commands],
                "feasibility_checks": [
                    row.model_dump() if hasattr(row, "model_dump") else row for row in feasibility_checks
                ],
            }
        )

    def _extract_player_intent_with_ollama(
        self, user_input: str, state_json: str, context_json: str, system_prompt: str
    ) -> PlayerIntentExtraction:
        prompt = (
            "Return JSON only with this shape: "
            '{"inventory":[{"action":"use|add|remove|steal|pickup|other","item_key":"string","quantity":1,'
            '"target_character":null,"owner":"self|target|scene|unknown"}],'
            '"commands":[{"type":"narrate|set_scene|adjust_hp|add_item|remove_item|set_item_state|set_flag|set_stat|'
            'add_effect|remove_effect","target":null,"key":null,"value":null,"amount":null,"text":null,'
            '"effect_category":null,"duration_turns":null}],'
            '"feasibility_checks":[{"action":"use|add|remove|steal|pickup|other","item_key":"string",'
            '"target_character":null,"question":"string","is_possible":true,"reason":"string",'
            '"object_type":"string","portability":"portable|non_portable|unknown",'
            '"requires_payment":null,"cost_amount":null,"currency":null,'
            '"payer_owner":"self|target|scene|unknown|null","has_required_funds":null,'
            '"acquisition_mode":"pickup|purchase|steal|loot|gift|craft|unknown|null","would_be_theft":null,'
            '"location_context":"string|null"}],'
            '"relevance_signals":[{"entity_type":"item|effect|scene|other","key":"string","context_tag":"string",'
            '"score":0.0,"reason":"string"}]}\n\n'
            "Extract concrete state-change intent implied by the player message, including inventory, hp, stats, "
            "flags, item states, and effects if present. Use commands for all intended state mutations. Also include "
            "feasibility questions and your best answer for each significant attempted action. "
            "Always extract item-usage attempts such as pull/draw/take/use/equip/cast with an item as inventory "
            "actions (`use`/`remove`) plus a matching feasibility check grounded in current inventory/context. "
            "Never return all arrays empty for non-empty input: if no structured mutation exists, emit one "
            "narrate command with the player's action text and one feasibility check with action='other'. "
            "Classify object type and portability (example: ruins -> structure/non_portable, stick -> small_object/portable).\n"
            "If an action is a transaction/purchase, include requires_payment, cost_amount, currency, payer_owner, "
            "and has_required_funds in feasibility_checks. For add/pickup attempts in shops/merchant contexts, "
            "set acquisition_mode and whether it would_be_theft if taken without purchase.\n"
            "Include relevance_signals for the most important item/effect/scene entities impacted by this input "
            "(score 0.0-1.0, where 1.0 is most important). Do not include weighting instructions or model controls.\n"
            f"state={state_json}\n"
            f"context={context_json}\n"
            f"user_input={user_input}"
        )
        payload = {
            "model": self._model_for_task("intent"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("intent"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=settings.ollama_timeout_s)
        content = body.get("message", {}).get("content", "")
        parsed = self._extract_json_object(content) if content else {"inventory": [], "commands": [], "feasibility_checks": []}
        return self._coerce_player_intent(parsed)

    def _extract_player_intent_with_openai(
        self, user_input: str, state_json: str, context_json: str, system_prompt: str
    ) -> PlayerIntentExtraction:
        prompt = (
            "Return JSON only with this shape: "
            '{"inventory":[{"action":"use|add|remove|steal|pickup|other","item_key":"string","quantity":1,'
            '"target_character":null,"owner":"self|target|scene|unknown"}],'
            '"commands":[{"type":"narrate|set_scene|adjust_hp|add_item|remove_item|set_item_state|set_flag|set_stat|'
            'add_effect|remove_effect","target":null,"key":null,"value":null,"amount":null,"text":null,'
            '"effect_category":null,"duration_turns":null}],'
            '"feasibility_checks":[{"action":"use|add|remove|steal|pickup|other","item_key":"string",'
            '"target_character":null,"question":"string","is_possible":true,"reason":"string",'
            '"object_type":"string","portability":"portable|non_portable|unknown",'
            '"requires_payment":null,"cost_amount":null,"currency":null,'
            '"payer_owner":"self|target|scene|unknown|null","has_required_funds":null,'
            '"acquisition_mode":"pickup|purchase|steal|loot|gift|craft|unknown|null","would_be_theft":null,'
            '"location_context":"string|null"}],'
            '"relevance_signals":[{"entity_type":"item|effect|scene|other","key":"string","context_tag":"string",'
            '"score":0.0,"reason":"string"}]}\n\n'
            "Always extract item-usage attempts such as pull/draw/take/use/equip/cast with an item as inventory "
            "actions (`use`/`remove`) plus a matching feasibility check grounded in current inventory/context. "
            "Extract concrete state-change intent implied by the player message, including inventory, hp, stats, "
            "flags, item states, and effects if present. Use commands for all intended state mutations. "
            "If an action is a transaction/purchase, include requires_payment, cost_amount, currency, payer_owner, "
            "and has_required_funds in feasibility_checks. For add/pickup attempts in shops/merchant contexts, "
            "set acquisition_mode and whether it would_be_theft if taken without purchase.\n"
            f"state={state_json}\n"
            f"context={context_json}\n"
            f"user_input={user_input}"
        )
        parsed = self._chat_json_with_openai(task="intent", system_prompt=system_prompt, user_prompt=prompt)
        return self._coerce_player_intent(parsed if parsed else {"inventory": [], "commands": [], "feasibility_checks": []})

    def extract_player_intent(
        self, user_input: str, state_json: str, context_json: str, system_prompt: str
    ) -> PlayerIntentExtraction:
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                return self._enrich_intent_from_text(
                    user_input,
                    self._fallback_extract_player_intent(user_input, state_json),
                    emergency_fallback=True,
                )
            try:
                raw = self._extract_player_intent_with_ollama(user_input, state_json, context_json, system_prompt)
                self._record_provider_success("ollama")
                return self._enrich_intent_from_text(user_input, raw, emergency_fallback=False)
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] Intent extraction via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            if self._circuit_is_open("openai"):
                return self._enrich_intent_from_text(
                    user_input,
                    self._fallback_extract_player_intent(user_input, state_json),
                    emergency_fallback=True,
                )
            try:
                raw = self._extract_player_intent_with_openai(user_input, state_json, context_json, system_prompt)
                self._record_provider_success("openai")
                return self._enrich_intent_from_text(user_input, raw, emergency_fallback=False)
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] Intent extraction via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return self._enrich_intent_from_text(
            user_input,
            self._fallback_extract_player_intent(user_input, state_json),
            emergency_fallback=True,
        )

    @classmethod
    def _fallback_inventory_action_feasibility(cls, action: str, item_key: str, scene: str) -> dict:
        object_type, portability = cls._classify_object(item_key)
        if action in {"pickup", "add"} and portability == "non_portable":
            return {
                "is_possible": False,
                "reason": f"'{item_key}' is a {object_type} and is not portable.",
                "object_type": object_type,
                "portability": portability,
                "confidence": 0.95,
            }
        if action == "pickup" and not (scene or "").strip():
            return {
                "is_possible": False,
                "reason": "Scene context is missing for pickup feasibility.",
                "object_type": object_type,
                "portability": portability,
                "confidence": 0.6,
            }
        return {
            "is_possible": True,
            "reason": "No hard contradiction detected in fallback feasibility check.",
            "object_type": object_type,
            "portability": portability,
            "confidence": 0.4,
        }

    def _inventory_action_feasibility_with_ollama(
        self,
        *,
        action: str,
        item_key: str,
        scene: str,
        user_input: str,
        state_json: str,
        context_json: str,
    ) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"is_possible":true,"reason":"string","object_type":"string","portability":"portable|non_portable|unknown",'
            '"confidence":0.0,"requires_payment":null,"cost_amount":null,"currency":null,"would_be_theft":null,'
            '"acquisition_mode":"pickup|purchase|steal|loot|gift|craft|unknown|null"}\n\n'
            "Determine whether the requested inventory action is feasible right now given scene/state/context.\n"
            f"action={action}\n"
            f"item_key={item_key}\n"
            f"scene={scene}\n"
            f"user_input={user_input}\n"
            f"state={state_json}\n"
            f"context={context_json}"
        )
        payload = {
            "model": self._model_for_task("intent"),
            "messages": [
                {"role": "system", "content": "You are a strict RPG feasibility checker."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("intent"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=settings.ollama_timeout_s)
        parsed = self._extract_json_object(body.get("message", {}).get("content", "") or "{}")
        fallback = self._fallback_inventory_action_feasibility(action, item_key, scene)
        return {
            "is_possible": bool(parsed.get("is_possible", fallback["is_possible"])),
            "reason": str(parsed.get("reason", fallback["reason"]) or fallback["reason"]),
            "object_type": str(parsed.get("object_type", fallback["object_type"]) or fallback["object_type"]),
            "portability": str(parsed.get("portability", fallback["portability"]) or fallback["portability"]),
            "confidence": float(parsed.get("confidence", fallback["confidence"]) or fallback["confidence"]),
            "requires_payment": (
                parsed.get("requires_payment")
                if isinstance(parsed.get("requires_payment"), bool) or parsed.get("requires_payment") is None
                else None
            ),
            "cost_amount": (
                int(parsed.get("cost_amount"))
                if parsed.get("cost_amount") not in (None, "", "null")
                and str(parsed.get("cost_amount", "")).strip().lstrip("-").isdigit()
                else None
            ),
            "currency": str(parsed.get("currency", "")).strip().lower() or None,
            "would_be_theft": (
                parsed.get("would_be_theft")
                if isinstance(parsed.get("would_be_theft"), bool) or parsed.get("would_be_theft") is None
                else None
            ),
            "acquisition_mode": (
                str(parsed.get("acquisition_mode", "")).strip().lower()
                if str(parsed.get("acquisition_mode", "")).strip().lower()
                in {"pickup", "purchase", "steal", "loot", "gift", "craft", "unknown"}
                else None
            ),
        }

    def _inventory_action_feasibility_with_openai(
        self,
        *,
        action: str,
        item_key: str,
        scene: str,
        user_input: str,
        state_json: str,
        context_json: str,
    ) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"is_possible":true,"reason":"string","object_type":"string","portability":"portable|non_portable|unknown",'
            '"confidence":0.0,"requires_payment":null,"cost_amount":null,"currency":null,"would_be_theft":null,'
            '"acquisition_mode":"pickup|purchase|steal|loot|gift|craft|unknown|null"}\n\n'
            "Determine whether the requested inventory action is feasible right now given scene/state/context.\n"
            f"action={action}\n"
            f"item_key={item_key}\n"
            f"scene={scene}\n"
            f"user_input={user_input}\n"
            f"state={state_json}\n"
            f"context={context_json}"
        )
        parsed = self._chat_json_with_openai(
            task="intent",
            system_prompt="You are a strict RPG feasibility checker.",
            user_prompt=prompt,
        )
        fallback = self._fallback_inventory_action_feasibility(action, item_key, scene)
        return {
            "is_possible": bool(parsed.get("is_possible", fallback["is_possible"])),
            "reason": str(parsed.get("reason", fallback["reason"]) or fallback["reason"]),
            "object_type": str(parsed.get("object_type", fallback["object_type"]) or fallback["object_type"]),
            "portability": str(parsed.get("portability", fallback["portability"]) or fallback["portability"]),
            "confidence": float(parsed.get("confidence", fallback["confidence"]) or fallback["confidence"]),
            "requires_payment": (
                parsed.get("requires_payment")
                if isinstance(parsed.get("requires_payment"), bool) or parsed.get("requires_payment") is None
                else None
            ),
            "cost_amount": (
                int(parsed.get("cost_amount"))
                if parsed.get("cost_amount") not in (None, "", "null")
                and str(parsed.get("cost_amount", "")).strip().lstrip("-").isdigit()
                else None
            ),
            "currency": str(parsed.get("currency", "")).strip().lower() or None,
            "would_be_theft": (
                parsed.get("would_be_theft")
                if isinstance(parsed.get("would_be_theft"), bool) or parsed.get("would_be_theft") is None
                else None
            ),
            "acquisition_mode": (
                str(parsed.get("acquisition_mode", "")).strip().lower()
                if str(parsed.get("acquisition_mode", "")).strip().lower()
                in {"pickup", "purchase", "steal", "loot", "gift", "craft", "unknown"}
                else None
            ),
        }

    def assess_inventory_action_feasibility(
        self,
        *,
        action: str,
        item_key: str,
        scene: str,
        user_input: str,
        state_json: str,
        context_json: str,
    ) -> dict:
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                return self._fallback_inventory_action_feasibility(action, item_key, scene)
            try:
                out = self._inventory_action_feasibility_with_ollama(
                    action=action,
                    item_key=item_key,
                    scene=scene,
                    user_input=user_input,
                    state_json=state_json,
                    context_json=context_json,
                )
                self._record_provider_success("ollama")
                return out
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] Feasibility check via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            if self._circuit_is_open("openai"):
                return self._fallback_inventory_action_feasibility(action, item_key, scene)
            try:
                out = self._inventory_action_feasibility_with_openai(
                    action=action,
                    item_key=item_key,
                    scene=scene,
                    user_input=user_input,
                    state_json=state_json,
                    context_json=context_json,
                )
                self._record_provider_success("openai")
                return out
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] Feasibility check via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return self._fallback_inventory_action_feasibility(action, item_key, scene)

    @staticmethod
    def _fallback_review_output(narration: str) -> OutputReview:
        return OutputReview(
            plausible=True,
            breaks_pc_autonomy=False,
            violations=[],
            revised_narration=narration,
            input_aligned=True,
            alignment_score=1.0,
        )

    def _review_output_with_ollama(
        self,
        user_input: str,
        narration: str,
        state_json: str,
        context_json: str,
        system_prompt: str,
    ) -> OutputReview:
        prompt = (
            "Return JSON only with this shape: "
            '{"plausible":true,"breaks_pc_autonomy":false,"violations":["string"],'
            '"revised_narration":"string","input_aligned":true,"alignment_score":1.0}\n\n'
            "Review the narration against world plausibility, player-character autonomy rules, and campaign/system "
            "rules. Also assess whether narration is meaningfully responsive to the specific user_input, "
            "setting input_aligned and alignment_score (0.0-1.0). If there are violations, provide a corrected revised_narration. If no violations, "
            "revised_narration should equal input narration.\n"
            f"state={state_json}\n"
            f"context={context_json}\n"
            f"user_input={user_input}\n"
            f"narration={narration}\n"
            f"system_prompt={system_prompt}"
        )
        payload = {
            "model": self._model_for_task("review"),
            "messages": [
                {"role": "system", "content": "You are a strict narrative compliance reviewer."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("review"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=settings.ollama_timeout_s)
        content = body.get("message", {}).get("content", "")
        parsed = self._extract_json_object(content) if content else {}
        review = OutputReview.model_validate(parsed)
        if not review.revised_narration.strip():
            review.revised_narration = narration
        return review

    def _review_output_with_openai(
        self,
        user_input: str,
        narration: str,
        state_json: str,
        context_json: str,
        system_prompt: str,
    ) -> OutputReview:
        prompt = (
            "Return JSON only with this shape: "
            '{"plausible":true,"breaks_pc_autonomy":false,"violations":["string"],'
            '"revised_narration":"string","input_aligned":true,"alignment_score":1.0}\n\n'
            f"state={state_json}\n"
            f"context={context_json}\n"
            f"user_input={user_input}\n"
            f"narration={narration}\n"
            f"system_prompt={system_prompt}"
        )
        parsed = self._chat_json_with_openai(
            task="review",
            system_prompt="You are a strict narrative compliance reviewer.",
            user_prompt=prompt,
        )
        review = OutputReview.model_validate(parsed if parsed else {})
        if not review.revised_narration.strip():
            review.revised_narration = narration
        return review

    def review_output(
        self,
        user_input: str,
        narration: str,
        state_json: str,
        context_json: str,
        system_prompt: str,
    ) -> OutputReview:
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                return self._fallback_review_output(narration)
            try:
                out = self._review_output_with_ollama(user_input, narration, state_json, context_json, system_prompt)
                self._record_provider_success("ollama")
                return out
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] Output review via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            if self._circuit_is_open("openai"):
                return self._fallback_review_output(narration)
            try:
                out = self._review_output_with_openai(user_input, narration, state_json, context_json, system_prompt)
                self._record_provider_success("openai")
                return out
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] Output review via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return self._fallback_review_output(narration)

    @staticmethod
    def _fallback_infer_discord_command(user_input: str, possible_commands: list[str]) -> dict:
        entered = user_input.strip().split()[0].lower() if user_input.strip() else ""
        if not entered.startswith("!"):
            return {"matched_command": None, "confidence": 0.0, "reason": "not_a_command"}
        norm_commands = sorted({c.lower() for c in possible_commands if c.startswith("!")})
        if entered in norm_commands:
            return {"matched_command": entered, "confidence": 1.0, "reason": "exact_match"}
        match = get_close_matches(entered, norm_commands, n=1, cutoff=0.6)
        if match:
            return {"matched_command": match[0], "confidence": 0.75, "reason": "string_similarity"}
        return {"matched_command": None, "confidence": 0.0, "reason": "no_match"}

    @staticmethod
    def _fallback_classify_self_query_intent(user_input: str) -> dict:
        text = (user_input or "").strip().lower()
        if not text:
            return {"intent": "none", "confidence": 0.0, "reason": "empty"}
        appearance_markers = (
            "what do i look like",
            "how do i look",
            "describe me",
            "my appearance",
            "what am i wearing",
        )
        equipment_markers = (
            "what am i equipped with",
            "what am i equipped wi th",
            "what am i carrying",
            "what do i have equipped",
            "show my inventory",
            "what is in my inventory",
            "what do i have in my inventory",
            "what am i holding",
        )
        if any(p in text for p in appearance_markers):
            return {"intent": "appearance", "confidence": 0.95, "reason": "fallback_phrase_match"}
        if any(p in text for p in equipment_markers):
            return {"intent": "equipment", "confidence": 0.95, "reason": "fallback_phrase_match"}
        return {"intent": "none", "confidence": 0.0, "reason": "no_match"}

    def _classify_self_query_intent_with_ollama(self, user_input: str) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"intent":"appearance|equipment|none","confidence":0.0,"reason":"string"}\n\n'
            "Classify whether this player message is asking to inspect their own character appearance or equipment/inventory.\n"
            f"input={user_input}"
        )
        payload = {
            "model": self._model_for_task("intent"),
            "messages": [
                {"role": "system", "content": "You classify player self-query intents."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("intent"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=max(8, min(20, settings.ollama_timeout_s)))
        content = body.get("message", {}).get("content", "")
        parsed = self._extract_json_object(content) if content else {}
        intent = str(parsed.get("intent", "") or "none").strip().lower()
        if intent not in {"appearance", "equipment", "none"}:
            intent = "none"
        confidence = parsed.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(parsed.get("reason", "") or "").strip() or "llm_inference"
        return {"intent": intent, "confidence": max(0.0, min(1.0, confidence)), "reason": reason}

    def _classify_self_query_intent_with_openai(self, user_input: str) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"intent":"appearance|equipment|none","confidence":0.0,"reason":"string"}\n\n'
            f"input={user_input}"
        )
        parsed = self._chat_json_with_openai(
            task="intent",
            system_prompt="You classify player self-query intents.",
            user_prompt=prompt,
        )
        intent = str(parsed.get("intent", "") or "none").strip().lower()
        if intent not in {"appearance", "equipment", "none"}:
            intent = "none"
        confidence = parsed.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(parsed.get("reason", "") or "").strip() or "llm_inference"
        return {"intent": intent, "confidence": max(0.0, min(1.0, confidence)), "reason": reason}

    def classify_self_query_intent(self, user_input: str) -> dict:
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                return self._fallback_classify_self_query_intent(user_input)
            try:
                out = self._classify_self_query_intent_with_ollama(user_input)
                if out.get("intent") in {"appearance", "equipment"}:
                    self._record_provider_success("ollama")
                    return out
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] Self-query classification via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            if self._circuit_is_open("openai"):
                return self._fallback_classify_self_query_intent(user_input)
            try:
                out = self._classify_self_query_intent_with_openai(user_input)
                if out.get("intent") in {"appearance", "equipment"}:
                    self._record_provider_success("openai")
                    return out
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] Self-query classification via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return self._fallback_classify_self_query_intent(user_input)

    def _infer_discord_command_with_ollama(self, user_input: str, possible_commands: list[str]) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"matched_command":"!name_or_null","confidence":0.0,"reason":"string"}\n\n'
            "You map a user-entered Discord command to one of the allowed commands. "
            "Use null when no command is a good match.\n"
            f"entered={user_input}\n"
            f"possible_commands={json.dumps(possible_commands)}"
        )
        payload = {
            "model": self._model_for_task("intent"),
            "messages": [
                {"role": "system", "content": "You are a strict command router for Discord bots."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("intent"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=max(8, min(20, settings.ollama_timeout_s)))
        content = body.get("message", {}).get("content", "")
        parsed = self._extract_json_object(content) if content else {}
        matched = parsed.get("matched_command")
        if isinstance(matched, str):
            matched = matched.strip().lower() or None
        elif matched is not None:
            matched = str(matched).strip().lower() or None
        confidence = parsed.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(parsed.get("reason", "") or "").strip() or "llm_inference"
        allowed = {c.lower() for c in possible_commands if c.startswith("!")}
        if matched not in allowed:
            matched = None
            confidence = 0.0
        return {"matched_command": matched, "confidence": max(0.0, min(1.0, confidence)), "reason": reason}

    def _infer_discord_command_with_openai(self, user_input: str, possible_commands: list[str]) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"matched_command":"!name_or_null","confidence":0.0,"reason":"string"}\n\n'
            f"entered={user_input}\n"
            f"possible_commands={json.dumps(possible_commands)}"
        )
        parsed = self._chat_json_with_openai(
            task="intent",
            system_prompt="You are a strict command router for Discord bots.",
            user_prompt=prompt,
        )
        matched = parsed.get("matched_command")
        if isinstance(matched, str):
            matched = matched.strip().lower() or None
        elif matched is not None:
            matched = str(matched).strip().lower() or None
        confidence = parsed.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(parsed.get("reason", "") or "").strip() or "llm_inference"
        allowed = {c.lower() for c in possible_commands if c.startswith("!")}
        if matched not in allowed:
            matched = None
            confidence = 0.0
        return {"matched_command": matched, "confidence": max(0.0, min(1.0, confidence)), "reason": reason}

    def infer_discord_command(self, user_input: str, possible_commands: list[str]) -> dict:
        min_conf = max(0.0, min(1.0, float(settings.command_suggestion_min_confidence)))

        def _acceptable(guess: dict) -> bool:
            matched = str(guess.get("matched_command", "") or "").strip()
            if not matched:
                return False
            try:
                conf = float(guess.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            return conf >= min_conf

        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                return self._fallback_infer_discord_command(user_input, possible_commands)
            try:
                guessed = self._infer_discord_command_with_ollama(user_input, possible_commands)
                if _acceptable(guessed):
                    self._record_provider_success("ollama")
                    return guessed
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] Command inference via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            if self._circuit_is_open("openai"):
                return self._fallback_infer_discord_command(user_input, possible_commands)
            try:
                guessed = self._infer_discord_command_with_openai(user_input, possible_commands)
                if _acceptable(guessed):
                    self._record_provider_success("openai")
                    return guessed
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] Command inference via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return self._fallback_infer_discord_command(user_input, possible_commands)

    @staticmethod
    def _fallback_world_seed_payload(mode: str) -> dict:
        if mode == "story":
            return {
                "scene_short": "Neon-lit skyport market under a light rain.",
                "scene_intro": (
                    "Rain taps across neon signs as rumors spread through a crowded skyport market, "
                    "and strangers trade secrets in the glow of docked airships."
                ),
                "locations": {
                    "Skyport Market": {
                        "description": "A crowded market under hanging neon and rain-slick canvas roofs.",
                        "tags": ["urban", "trade", "crowded"],
                    },
                    "Dockside Alleys": {
                        "description": "Narrow passageways where whispers travel faster than footsteps.",
                        "tags": ["urban", "shadowed"],
                    },
                },
                "npcs": {
                    "Dockmaster Ilya": {
                        "description": "A practical coordinator keeping airship berths moving.",
                        "disposition": "neutral",
                        "location": "Skyport Market",
                    }
                },
            }
        return {
            "scene_short": "Frontier town beside ancient ruins at dawn.",
            "scene_intro": (
                "Dawn breaks over a frontier town built beside ancient ruins, where merchants raise shutters, "
                "watchmen trade shifts, and old stones seem to hum with forgotten power."
            ),
            "locations": {
                "Frontier Square": {
                    "description": "The central square where caravans unload and rumors spread at first light.",
                    "tags": ["town", "social", "market"],
                },
                "Ancient Ruins": {
                    "description": "Weathered stone corridors outside town, etched with unfamiliar glyphs.",
                    "tags": ["ruins", "danger", "exploration"],
                },
            },
            "npcs": {
                "Captain Varek": {
                    "description": "Town watch captain with a careful eye for trouble.",
                    "disposition": "guarded",
                    "location": "Frontier Square",
                }
            },
        }

    def _generate_world_seed_with_ollama(self, mode: str) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"scene_short":"string","scene_intro":"string","locations":{"name":{"description":"string","tags":["string"]}},"npcs":{"name":{"description":"string","disposition":"string","location":"string"}}}\n\n'
            "Generate a campaign opening scene.\n"
            "- scene_short: concise world-state line for prompts (max 14 words)\n"
            "- scene_intro: descriptive player-facing opener (2-3 sentences)\n"
            "- locations: 1-3 key starting locations\n"
            "- npcs: 0-2 key starting NPCs\n"
            "- Output must be English only unless explicitly asked otherwise (not requested here).\n"
            f"mode={mode}"
        )
        payload = {
            "model": self._model_for_task("narration"),
            "messages": [
                {"role": "system", "content": "You are a concise worldbuilding assistant for tabletop campaigns."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": self._options_for_task("narration"),
        }
        if settings.llm_json_mode_strict:
            payload["format"] = "json"
        req = request.Request(
            url=f"{settings.ollama_url.rstrip('/')}/api/chat",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        body = self._urlopen_json_with_retry(req, timeout_s=settings.ollama_timeout_s)
        content = body.get("message", {}).get("content", "")
        parsed = self._extract_json_object(content) if content else {}
        short = str(parsed.get("scene_short", "") or "").strip()
        intro = str(parsed.get("scene_intro", "") or "").strip()
        if not short or not intro:
            fallback = self._fallback_world_seed_payload(mode)
            short = short or fallback["scene_short"]
            intro = intro or fallback["scene_intro"]
        if self._contains_non_english_script(short) or self._contains_non_english_script(intro):
            fallback = self._fallback_world_seed_payload(mode)
            short = fallback["scene_short"]
            intro = fallback["scene_intro"]
        short = re.sub(r"\s+", " ", short).strip()
        intro = re.sub(r"\s+", " ", intro).strip()
        # Hard cap to keep prompt token cost down.
        short_words = short.split()
        if len(short_words) > 14:
            short = " ".join(short_words[:14]).rstrip(".,;:!?")
        return {
            "scene_short": short,
            "scene_intro": intro,
            "locations": dict(parsed.get("locations", {}) or {}),
            "npcs": dict(parsed.get("npcs", {}) or {}),
        }

    def _generate_world_seed_with_openai(self, mode: str) -> dict:
        prompt = (
            "Return JSON only with this shape: "
            '{"scene_short":"string","scene_intro":"string","locations":{"name":{"description":"string","tags":["string"]}},"npcs":{"name":{"description":"string","disposition":"string","location":"string"}}}\n\n'
            f"mode={mode}"
        )
        parsed = self._chat_json_with_openai(
            task="narration",
            system_prompt="You are a concise worldbuilding assistant for tabletop campaigns.",
            user_prompt=prompt,
        )
        short = str(parsed.get("scene_short", "") or "").strip()
        intro = str(parsed.get("scene_intro", "") or "").strip()
        if not short or not intro:
            fallback = self._fallback_world_seed_payload(mode)
            short = short or fallback["scene_short"]
            intro = intro or fallback["scene_intro"]
        return {
            "scene_short": short,
            "scene_intro": intro,
            "locations": dict(parsed.get("locations", {}) or {}),
            "npcs": dict(parsed.get("npcs", {}) or {}),
        }

    @staticmethod
    def _normalize_world_seed_entities(payload: dict) -> tuple[dict[str, dict], dict[str, dict]]:
        raw_locations = dict(payload.get("locations", {}) or {})
        raw_npcs = dict(payload.get("npcs", {}) or {})

        locations: dict[str, dict] = {}
        for key, value in raw_locations.items():
            name = str(key).strip()
            if not name:
                continue
            row = dict(value or {}) if isinstance(value, dict) else {}
            row["name"] = str(row.get("name", "") or name)
            row["description"] = str(row.get("description", "") or "")
            tags = row.get("tags", [])
            if not isinstance(tags, list):
                tags = [str(tags)]
            row["tags"] = [str(t).strip() for t in tags if str(t).strip()]
            if "connected_to" in row and not isinstance(row.get("connected_to"), list):
                row["connected_to"] = [str(row.get("connected_to"))]
            locations[name] = row

        npcs: dict[str, dict] = {}
        for key, value in raw_npcs.items():
            name = str(key).strip()
            if not name:
                continue
            row = dict(value or {}) if isinstance(value, dict) else {}
            row["name"] = str(row.get("name", "") or name)
            row["description"] = str(row.get("description", "") or "")
            row["disposition"] = str(row.get("disposition", "") or "neutral")
            row["location"] = str(row.get("location", "") or "")
            npcs[name] = row

        return locations, npcs

    def generate_world_seed(self, mode: str) -> WorldState:
        payload = self._fallback_world_seed_payload(mode)
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                locations, npcs = self._normalize_world_seed_entities(payload)
                return WorldState(scene=payload["scene_short"], flags={"mode": mode, "scene_intro": payload["scene_intro"]}, party={}, npcs=npcs, locations=locations, combat_round=None)
            try:
                payload = self._generate_world_seed_with_ollama(mode)
                self._record_provider_success("ollama")
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] World seed via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            if self._circuit_is_open("openai"):
                locations, npcs = self._normalize_world_seed_entities(payload)
                return WorldState(scene=payload["scene_short"], flags={"mode": mode, "scene_intro": payload["scene_intro"]}, party={}, npcs=npcs, locations=locations, combat_round=None)
            try:
                payload = self._generate_world_seed_with_openai(mode)
                self._record_provider_success("openai")
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] World seed via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")

        locations, npcs = self._normalize_world_seed_entities(payload)
        return WorldState(
            scene=payload["scene_short"],
            flags={"mode": mode, "scene_intro": payload["scene_intro"]},
            party={},
            npcs=npcs,
            locations=locations,
            combat_round=None,
        )

    def generate_character_from_description(self, description: str, fallback_name: str) -> CharacterState:
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            if self._circuit_is_open("ollama"):
                return self._coerce_character_profile({}, description, fallback_name)
            try:
                out = self._character_profile_with_ollama(description, fallback_name)
                self._record_provider_success("ollama")
                return out
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                self._record_provider_failure("ollama")
                print(f"[LLMAdapter] Character extraction via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            if self._circuit_is_open("openai"):
                return self._coerce_character_profile({}, description, fallback_name)
            try:
                out = self._character_profile_with_openai(description, fallback_name)
                self._record_provider_success("openai")
                return out
            except Exception as exc:  # noqa: BLE001
                self._record_provider_failure("openai")
                print(f"[LLMAdapter] Character extraction via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return self._coerce_character_profile({}, description, fallback_name)
