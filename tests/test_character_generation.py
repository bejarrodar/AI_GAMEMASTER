from aigm.adapters.llm import LLMAdapter


def test_extract_name_from_my_name_is() -> None:
    llm = LLMAdapter()
    c = llm.generate_character_from_description("My name is Aria. I am a ranger.", fallback_name="PlayerOne")
    assert c.name == "Aria"


def test_extract_name_from_name_field() -> None:
    llm = LLMAdapter()
    c = llm.generate_character_from_description("name: Kael Stormborn, rogue with a dark past", fallback_name="P2")
    assert c.name == "Kael Stormborn"


def test_extract_name_from_named_phrase() -> None:
    llm = LLMAdapter()
    c = llm.generate_character_from_description("A dwarf fighter named Brom Ironfist with a heavy axe", "P3")
    assert c.name == "Brom Ironfist"


def test_fallback_name_when_not_provided() -> None:
    llm = LLMAdapter()
    c = llm.generate_character_from_description("I am a wizard from the crystal coast.", fallback_name="Jarrod")
    assert c.name == "Jarrod"


def test_character_description_and_stick_item_seeded() -> None:
    llm = LLMAdapter()
    prompt = (
        "I am Bear a druid who wishes he could turn into a bear but in all actuality "
        "I am just a normal guy that can't do anything. I believe I am a master of all "
        "things druid but I just have a stick that I hit people with."
    )
    c = llm.generate_character_from_description(prompt, fallback_name="Player")
    assert c.name == "Bear"
    assert c.description == prompt
    assert c.inventory.get("stick") == 1
