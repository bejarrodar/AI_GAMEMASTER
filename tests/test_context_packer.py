from aigm.core.context_builder import ContextBuilder
from aigm.schemas.game import CharacterState, LocationState, NPCState, WorldState


def test_pack_for_llm_limits_facts_and_turns() -> None:
    builder = ContextBuilder()
    state = WorldState(
        scene="Frontier town beside ruins.",
        flags={"mode": "story", "weather": "rain", "time": "dawn"},
        party={
            "Shade": CharacterState(name="Shade", hp=10, max_hp=10, inventory={"coin": 3}),
            "Bear": CharacterState(name="Bear", hp=10, max_hp=10, inventory={}),
        },
    )
    turns = []
    for i in range(10):
        turns.append(
            {
                "turn_id": i + 1,
                "actor_name": f"P{i}",
                "user_input": f"user input {i}",
                "narration": f"narration {i}",
            }
        )
    packed = builder.pack_for_llm(
        base_context={"campaign_id": 1, "mode": "story", "conversation_history": turns},
        state=state,
        actor_character_name="Shade",
        max_facts=5,
        recent_turns=3,
        turn_line_max_chars=80,
    )
    assert len(packed["relevant_facts"]) <= 5
    summary_lines = [x for x in str(packed["turn_summary"]).splitlines() if x.strip()]
    assert len(summary_lines) <= 3
    assert "Shade" in " ".join(packed["relevant_facts"])
    assert len(packed["recent_turns"]) <= 3
    assert "context_budget" in packed


def test_pack_for_llm_includes_runtime_constraints() -> None:
    builder = ContextBuilder()
    state = WorldState(scene="Town square", flags={}, party={})
    packed = builder.pack_for_llm(
        base_context={
            "campaign_id": 1,
            "mode": "dnd",
            "runtime_constraints": ["No player can be put into inventory."],
            "conversation_history": [],
        },
        state=state,
        actor_character_name=None,
    )
    assert packed["runtime_constraints"] == ["No player can be put into inventory."]
    assert any("Constraint:" in f for f in packed["relevant_facts"])


def test_pack_for_llm_applies_token_budget_and_reports_truncation() -> None:
    builder = ContextBuilder()
    state = WorldState(
        scene="Frontier town beside ruins with many moving parts.",
        flags={"mode": "story", "weather": "rain", "time": "dawn"},
        party={
            "Shade": CharacterState(name="Shade", hp=10, max_hp=10, inventory={"torch": 1, "dagger": 1, "coin": 12}),
            "Bear": CharacterState(name="Bear", hp=10, max_hp=10, inventory={"staff": 1}),
        },
    )
    turns = []
    for i in range(8):
        turns.append(
            {
                "turn_id": i + 1,
                "actor_name": f"P{i}",
                "user_input": f"user input {i} with extra verbosity",
                "narration": f"narration {i} with extra verbosity to force truncation",
            }
        )
    packed = builder.pack_for_llm(
        base_context={"campaign_id": 1, "mode": "story", "conversation_history": turns},
        state=state,
        actor_character_name="Shade",
        max_facts=10,
        recent_turns=6,
        turn_line_max_chars=120,
        token_budget_chars=260,
        include_truncation_diagnostics=True,
    )
    budget = packed.get("context_budget", {})
    assert bool(budget.get("truncated")) is True
    assert int(budget.get("estimated_chars_after", 0)) <= int(budget.get("budget_chars", 0))
    assert int(budget.get("dropped_facts", 0)) >= 0


def test_torch_relevance_higher_in_dark_context_than_daylight() -> None:
    builder = ContextBuilder()
    base = {"campaign_id": 1, "mode": "story", "conversation_history": []}
    dark_state = WorldState(
        scene="A dark forest path at night.",
        flags={"mode": "story"},
        party={"Shade": CharacterState(name="Shade", hp=10, max_hp=10, inventory={"torch": 1})},
    )
    day_state = WorldState(
        scene="A sunny town square at noon.",
        flags={"mode": "story"},
        party={"Shade": CharacterState(name="Shade", hp=10, max_hp=10, inventory={"torch": 1})},
    )
    packed_dark = builder.pack_for_llm(
        base_context=base,
        state=dark_state,
        actor_character_name="Shade",
        user_input="I look around",
        max_facts=12,
    )
    packed_day = builder.pack_for_llm(
        base_context=base,
        state=day_state,
        actor_character_name="Shade",
        user_input="I look around",
        max_facts=12,
    )
    dark_score = next(
        (f["score"] for f in packed_dark["relevance_scored_facts"] if f["fact"].startswith("Actor item: torch")),
        0.0,
    )
    day_score = next(
        (f["score"] for f in packed_day["relevance_scored_facts"] if f["fact"].startswith("Actor item: torch")),
        0.0,
    )
    assert dark_score > day_score


def test_pack_for_llm_applies_learned_item_relevance_boost() -> None:
    builder = ContextBuilder()
    state = WorldState(
        scene="Bright market street at noon.",
        flags={"mode": "story"},
        party={
            "Shade": CharacterState(
                name="Shade",
                hp=10,
                max_hp=10,
                inventory={"lantern": 1, "apple": 1},
            )
        },
    )
    base = {"campaign_id": 1, "mode": "story", "conversation_history": []}
    packed_plain = builder.pack_for_llm(
        base_context=base,
        state=state,
        actor_character_name="Shade",
        user_input="I look around the market",
        max_facts=12,
    )
    packed_learned = builder.pack_for_llm(
        base_context=base,
        state=state,
        actor_character_name="Shade",
        user_input="I look around the market",
        learned_item_relevance={"lantern": 2.0},
        max_facts=12,
    )
    plain_score = next(
        (f["score"] for f in packed_plain["relevance_scored_facts"] if f["fact"].startswith("Actor item: lantern")),
        0.0,
    )
    learned_score = next(
        (f["score"] for f in packed_learned["relevance_scored_facts"] if f["fact"].startswith("Actor item: lantern")),
        0.0,
    )
    assert learned_score > plain_score
    assert packed_learned["learned_item_relevance"]["lantern"] == 2.0


def test_pack_for_llm_includes_npc_and_location_facts() -> None:
    builder = ContextBuilder()
    state = WorldState(
        scene="Market dawn",
        party={"Shade": CharacterState(name="Shade", hp=10, max_hp=10)},
        npcs={
            "Captain Varek": NPCState(
                name="Captain Varek",
                description="Guard captain",
                disposition="guarded",
                location="Frontier Square",
            )
        },
        locations={
            "Frontier Square": LocationState(
                name="Frontier Square",
                description="Main square",
                tags=["town", "market"],
            )
        },
    )
    packed = builder.pack_for_llm(
        base_context={"campaign_id": 1, "mode": "dnd", "conversation_history": []},
        state=state,
        actor_character_name="Shade",
        user_input="I ask Captain Varek in the square for rumors",
        max_facts=12,
    )
    joined = " ".join(packed["relevant_facts"])
    assert "Captain Varek" in joined
    assert "Frontier Square" in joined
