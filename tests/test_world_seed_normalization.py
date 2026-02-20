from __future__ import annotations

from aigm.adapters.llm import LLMAdapter
from aigm.config import settings


def test_generate_world_seed_normalizes_missing_nested_names(monkeypatch) -> None:
    adapter = LLMAdapter()
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(
        adapter,
        "_generate_world_seed_with_ollama",
        lambda _mode: {
            "scene_short": "Ancient temple under storm clouds.",
            "scene_intro": "Thunder rolls above broken stone and drifting ash.",
            "locations": {
                "Temple of Kaelor": {"description": "A crumbling temple.", "tags": ["ruins", "mystic"]},
            },
            "npcs": {
                "Grym the Watcher": {"description": "An imposing sentinel.", "location": "Temple of Kaelor"},
            },
        },
    )

    state = adapter.generate_world_seed(mode="dnd")
    assert "Temple of Kaelor" in state.locations
    assert state.locations["Temple of Kaelor"].name == "Temple of Kaelor"
    assert "Grym the Watcher" in state.npcs
    assert state.npcs["Grym the Watcher"].name == "Grym the Watcher"
