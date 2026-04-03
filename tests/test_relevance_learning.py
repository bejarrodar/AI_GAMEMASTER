from aigm.services.game_service import GameService


def test_infer_item_semantics_non_portable_structure() -> None:
    object_type, portability = GameService._infer_item_semantics(
        "ancient_ruins",
        "I pick up the ancient ruins.",
        "Town square near old ruins.",
    )
    assert object_type == "structure"
    assert portability == "non_portable"


def test_infer_item_semantics_portable_item() -> None:
    object_type, portability = GameService._infer_item_semantics(
        "lantern",
        "I pull out my lantern.",
        "Dark alley at night.",
    )
    assert object_type == "item"
    assert portability == "portable"


def test_infer_effect_category_physical_from_poison() -> None:
    category = GameService._infer_effect_category(
        "poisoned",
        "The target suffers poison damage.",
        "Dark alley",
    )
    assert category == "physical"


def test_normalize_effect_key() -> None:
    assert GameService._normalize_effect_key("The Arcane Burn!") == "arcane_burn"
