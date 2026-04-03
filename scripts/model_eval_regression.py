from __future__ import annotations

import argparse
import json
from pathlib import Path

from aigm.adapters.llm import LLMAdapter
from aigm.services.game_service import GameService
from aigm.schemas.game import WorldState


def run_case_with_candidate(service: GameService, case: dict) -> dict:
    user_input = str(case.get("user_input", "")).strip()
    narration = str(case.get("candidate_narration", "")).strip()
    actor_name = str(case.get("actor_name", "")).strip() or None
    party_names = [str(x).strip() for x in list(case.get("party_names", []) or []) if str(x).strip()]
    min_alignment = float(case.get("min_alignment_score", 0.0) or 0.0)
    expect_input_aligned = bool(case.get("expect_input_aligned", True))
    expect_pc_autonomy_ok = bool(case.get("expect_pc_autonomy_ok", True))

    alignment = service._alignment_score(user_input, narration)
    input_aligned = not service._fails_input_probability_check(user_input, narration)
    autonomy_violation = service._narration_violates_other_player_agency(
        narration,
        actor_character_name=actor_name,
        player_character_names=party_names,
    )
    pc_autonomy_ok = autonomy_violation is None

    checks = {
        "alignment_min_ok": alignment >= min_alignment,
        "input_aligned_ok": input_aligned == expect_input_aligned,
        "pc_autonomy_ok": pc_autonomy_ok == expect_pc_autonomy_ok,
    }
    return {
        "alignment_score": round(float(alignment), 4),
        "input_aligned": bool(input_aligned),
        "pc_autonomy_ok": bool(pc_autonomy_ok),
        "autonomy_violation_name": autonomy_violation,
        "checks": checks,
        "passed": all(checks.values()),
    }


def generate_live_narration(llm: LLMAdapter, case: dict) -> str:
    state = WorldState(
        scene=str(case.get("scene", "Town square at dusk.")),
        flags={"mode": "story"},
        party={},
    )
    ai = llm.generate(
        user_input=str(case.get("user_input", "")),
        state_json=state.model_dump_json(),
        mode="story",
        context_json=json.dumps({"mode": "story", "campaign_id": 0}),
        system_prompt="You are a concise game master. Respond in English only.",
    )
    return ai.narration.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Model-evaluation regression harness for narration quality/safety.")
    parser.add_argument("--cases", default="scripts/model_eval_cases.json", help="Path to JSON case file.")
    parser.add_argument("--live-llm", action="store_true", help="Generate narration with current configured LLM.")
    parser.add_argument("--fail-on", type=int, default=1, help="Fail process when failures >= this number.")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    service = GameService(LLMAdapter())
    llm = LLMAdapter()
    results: list[dict] = []

    for case in cases:
        row = dict(case)
        if args.live_llm:
            row["candidate_narration"] = generate_live_narration(llm, row)
        eval_result = run_case_with_candidate(service, row)
        results.append(
            {
                "name": str(case.get("name", "unnamed")),
                "user_input": row.get("user_input"),
                "candidate_narration": row.get("candidate_narration"),
                **eval_result,
            }
        )

    failures = [r for r in results if not r["passed"]]
    print(json.dumps({"cases": results, "summary": {"total": len(results), "failed": len(failures)}}, indent=2))
    return 1 if len(failures) >= max(1, int(args.fail_on)) else 0


if __name__ == "__main__":
    raise SystemExit(main())

