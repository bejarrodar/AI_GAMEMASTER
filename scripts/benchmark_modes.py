from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import datetime

from aigm.schemas.game import AIResponse, OutputReview, PlayerIntentExtraction, WorldState


class DeterministicBenchmarkLLM:
    @staticmethod
    def generate_world_seed(mode: str = "story") -> WorldState:
        if mode == "dnd":
            scene = "Dawn breaks over a frontier town built beside ancient ruins."
        else:
            scene = "A new chapter opens in a bustling city square."
        return WorldState(
            scene=scene,
            flags={"mode": mode, "scene_short": scene[:120], "scene_intro": scene},
            party={},
        )

    @staticmethod
    def extract_player_intent(user_input: str, _state_json: str) -> PlayerIntentExtraction:
        return PlayerIntentExtraction.model_validate(
            {
                "inventory": [],
                "commands": [{"type": "narrate", "text": user_input}],
                "feasibility_checks": [],
            }
        )

    @staticmethod
    def generate(user_input: str, _state_json: str, _mode: str, _context_json: str, _system_prompt: str) -> AIResponse:
        return AIResponse.model_validate(
            {"narration": f"You attempt: {user_input}. The world responds.", "commands": []}
        )

    @staticmethod
    def review_output(
        _user_input: str,
        narration: str,
        _state_json: str,
        _context_json: str,
        _system_prompt: str,
    ) -> OutputReview:
        return OutputReview.model_validate(
            {
                "plausible": True,
                "breaks_pc_autonomy": False,
                "violations": [],
                "revised_narration": narration,
                "input_aligned": True,
                "alignment_score": 1.0,
            }
        )


def _run_case(
    *,
    label: str,
    mode: str,
    engine: str,
    turns: int,
    players: int,
    thread_id: str,
) -> dict:
    from aigm.db.bootstrap import ensure_schema
    from aigm.db.session import SessionLocal
    from aigm.services.game_service import GameService

    ensure_schema()
    service = GameService(DeterministicBenchmarkLLM())
    latencies_ms: list[float] = []
    failures = 0
    prompts = [
        "I look around for clues.",
        "I talk to nearby townsfolk.",
        "I inspect my gear.",
        "I move toward the market square.",
        "I ask about recent rumors.",
    ]

    with SessionLocal() as db:
        campaign = service.get_or_create_campaign(db, thread_id=thread_id, mode=mode)
        service.set_rule(db, campaign, "game_started", "true")
        service.set_rule(db, campaign, "turn_engine", engine)
        for i in range(max(1, turns)):
            actor_index = i % max(1, players)
            actor_id = f"bench_player_{actor_index}"
            actor_name = f"BenchPlayer{actor_index}"
            user_input = prompts[i % len(prompts)]
            started = time.perf_counter()
            try:
                service.process_turn_routed(
                    db,
                    campaign=campaign,
                    actor=actor_id,
                    actor_display_name=actor_name,
                    user_input=user_input,
                )
            except Exception:
                failures += 1
            latencies_ms.append((time.perf_counter() - started) * 1000.0)

    avg = statistics.mean(latencies_ms) if latencies_ms else 0.0
    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    p95 = statistics.quantiles(latencies_ms, n=20)[18] if len(latencies_ms) >= 20 else max(latencies_ms or [0.0])
    p99 = statistics.quantiles(latencies_ms, n=100)[98] if len(latencies_ms) >= 100 else max(latencies_ms or [0.0])
    return {
        "label": label,
        "mode": mode,
        "engine": engine,
        "turns": int(turns),
        "players": int(players),
        "thread_id": thread_id,
        "failures": int(failures),
        "latency_ms": {"avg": round(avg, 3), "p50": round(p50, 3), "p95": round(p95, 3), "p99": round(p99, 3)},
    }


def run_benchmark(*, turns: int, players: int, thread_prefix: str, database_url: str = "") -> dict:
    if database_url.strip():
        os.environ["AIGM_DATABASE_URL"] = database_url.strip()
    ts = int(time.time())
    cases = [
        ("dnd", "dnd", "classic"),
        ("story", "story", "classic"),
        ("crew", "dnd", "crew"),
    ]
    rows = []
    for label, mode, engine in cases:
        rows.append(
            _run_case(
                label=label,
                mode=mode,
                engine=engine,
                turns=turns,
                players=players,
                thread_id=f"{thread_prefix}-{label}-{ts}",
            )
        )
    return {"completed_at": f"{datetime.utcnow().isoformat()}Z", "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark turn throughput by mode (dnd, story, crew).")
    parser.add_argument("--turns", type=int, default=300, help="Turns per benchmark case.")
    parser.add_argument("--players", type=int, default=6, help="Simulated players per case.")
    parser.add_argument("--thread-prefix", default="bench", help="Campaign thread id prefix.")
    parser.add_argument("--database-url", default="", help="Optional DB URL override (e.g. sqlite:///:memory:).")
    parser.add_argument("--max-failures", type=int, default=0, help="Maximum failures allowed per case.")
    parser.add_argument("--max-p95-ms", type=float, default=5000.0, help="Maximum p95 latency allowed per case.")
    args = parser.parse_args()

    report = run_benchmark(
        turns=max(1, int(args.turns)),
        players=max(1, int(args.players)),
        thread_prefix=str(args.thread_prefix),
        database_url=str(args.database_url or ""),
    )
    print("[benchmark] summary")
    print(json.dumps(report, indent=2))

    ok = True
    for row in report["rows"]:
        if int(row["failures"]) > int(args.max_failures):
            print(
                f"[benchmark][FAIL] {row['label']} failures={row['failures']} exceeds max-failures={int(args.max_failures)}"
            )
            ok = False
        if float(row["latency_ms"]["p95"]) > float(args.max_p95_ms):
            print(
                f"[benchmark][FAIL] {row['label']} p95={row['latency_ms']['p95']} exceeds max-p95-ms={float(args.max_p95_ms)}"
            )
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
