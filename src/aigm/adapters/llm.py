from __future__ import annotations

import json
import re
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
            try:
                return self._generate_with_ollama(user_input, state_json, mode, context_json, system_prompt)
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print(f"[LLMAdapter] Ollama failed, using fallback: {type(exc).__name__}: {exc}")
                return self._fallback_response(user_input, state_json, mode)
        if provider == "openai":
            try:
                return self._generate_with_openai(user_input, state_json, mode, context_json, system_prompt)
            except Exception as exc:  # noqa: BLE001
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
            scene_text = ""
            try:
                scene_text = str(json.loads(state_json).get("scene", ""))
            except json.JSONDecodeError:
                scene_text = ""
            plausible = bool(
                scene_text and any(x in scene_text.lower() for x in ("forest", "woods", "trail", "town", "ruins", "market"))
            )
            object_type, portability = cls._classify_object(item_key)
            is_possible = plausible and portability != "non_portable"
            feasibility_checks.append(
                {
                    "action": "pickup",
                    "item_key": item_key,
                    "target_character": None,
                    "question": f"Can the player find '{item_key}' in the current scene?",
                    "is_possible": is_possible,
                    "reason": "Scene affordance heuristic from fallback extractor.",
                    "object_type": object_type,
                    "portability": portability,
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
    def _enrich_intent_from_text(cls, user_input: str, extracted: PlayerIntentExtraction) -> PlayerIntentExtraction:
        text = user_input.lower()
        inventory = list(extracted.inventory)
        commands = list(extracted.commands)
        feasibility_checks = list(extracted.feasibility_checks)

        def has_similar_inventory_intent(item_key: str, action: str) -> bool:
            for row in inventory:
                if row.item_key.lower() == item_key and row.action == action:
                    return True
            return False

        # Capture attempts like "put player Bear ... into inventory" even when model misses it.
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

        # Ensure each inventory intent has feasibility + type/portability metadata.
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
                    # Deterministic override for clearly non-portable items.
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
            '"object_type":"string","portability":"portable|non_portable|unknown"}],'
            '"relevance_signals":[{"entity_type":"item|effect|scene|other","key":"string","context_tag":"string",'
            '"score":0.0,"reason":"string"}]}\n\n'
            "Extract concrete state-change intent implied by the player message, including inventory, hp, stats, "
            "flags, item states, and effects if present. Use commands for all intended state mutations. Also include "
            "feasibility questions and your best answer for each significant attempted action. "
            "Never return all arrays empty for non-empty input: if no structured mutation exists, emit one "
            "narrate command with the player's action text and one feasibility check with action='other'. "
            "Classify object type and portability (example: ruins -> structure/non_portable, stick -> small_object/portable).\n"
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
            '"object_type":"string","portability":"portable|non_portable|unknown"}],'
            '"relevance_signals":[{"entity_type":"item|effect|scene|other","key":"string","context_tag":"string",'
            '"score":0.0,"reason":"string"}]}\n\n'
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
            try:
                raw = self._extract_player_intent_with_ollama(user_input, state_json, context_json, system_prompt)
                return self._enrich_intent_from_text(user_input, raw)
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print(f"[LLMAdapter] Intent extraction via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            try:
                raw = self._extract_player_intent_with_openai(user_input, state_json, context_json, system_prompt)
                return self._enrich_intent_from_text(user_input, raw)
            except Exception as exc:  # noqa: BLE001
                print(f"[LLMAdapter] Intent extraction via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return self._enrich_intent_from_text(user_input, self._fallback_extract_player_intent(user_input, state_json))

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
            try:
                return self._review_output_with_ollama(user_input, narration, state_json, context_json, system_prompt)
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print(f"[LLMAdapter] Output review via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            try:
                return self._review_output_with_openai(user_input, narration, state_json, context_json, system_prompt)
            except Exception as exc:  # noqa: BLE001
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
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            try:
                guessed = self._infer_discord_command_with_ollama(user_input, possible_commands)
                if guessed.get("matched_command"):
                    return guessed
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print(f"[LLMAdapter] Command inference via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            try:
                guessed = self._infer_discord_command_with_openai(user_input, possible_commands)
                if guessed.get("matched_command"):
                    return guessed
            except Exception as exc:  # noqa: BLE001
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

    def generate_world_seed(self, mode: str) -> WorldState:
        payload = self._fallback_world_seed_payload(mode)
        provider = settings.llm_provider.strip().lower()
        if provider == "ollama":
            try:
                payload = self._generate_world_seed_with_ollama(mode)
            except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print(f"[LLMAdapter] World seed via Ollama failed, using fallback: {type(exc).__name__}: {exc}")
        elif provider == "openai":
            try:
                payload = self._generate_world_seed_with_openai(mode)
            except Exception as exc:  # noqa: BLE001
                print(f"[LLMAdapter] World seed via OpenAI failed, using fallback: {type(exc).__name__}: {exc}")
        return WorldState(
            scene=payload["scene_short"],
            flags={"mode": mode, "scene_intro": payload["scene_intro"]},
            party={},
            npcs=dict(payload.get("npcs", {}) or {}),
            locations=dict(payload.get("locations", {}) or {}),
            combat_round=None,
        )

    def generate_character_from_description(self, description: str, fallback_name: str) -> CharacterState:
        lower_desc = description.lower()
        hp = 12 if "tank" in lower_desc else 10
        stats = {"str": 10, "dex": 10, "int": 10}
        if "wizard" in lower_desc or "mage" in lower_desc or "druid" in lower_desc:
            stats["int"] = 14
        if "rogue" in lower_desc or "thief" in lower_desc:
            stats["dex"] = 14
        if "fighter" in lower_desc or "knight" in lower_desc:
            stats["str"] = 14

        parsed_name = self._extract_character_name(description)
        name = parsed_name if parsed_name else fallback_name

        inventory: dict[str, int] = {}
        if "stick" in lower_desc:
            inventory["stick"] = 1
        elif "staff" in lower_desc:
            inventory["staff"] = 1
        elif "club" in lower_desc or "cudgel" in lower_desc:
            inventory["club"] = 1

        return CharacterState(
            name=name,
            description=description.strip(),
            hp=hp,
            max_hp=hp,
            stats=stats,
            inventory=inventory,
            item_states={},
            effects=[],
        )
