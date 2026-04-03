from __future__ import annotations

import argparse
import os
import statistics
import time
import tracemalloc
from datetime import datetime

from aigm.schemas.game import AIResponse, OutputReview, PlayerIntentExtraction, WorldState


class DeterministicSoakLLM:
    """Fast, deterministic adapter for soak runs that should not depend on external model services."""

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
        narration = f"You attempt: {user_input}. The world responds and the story advances."
        return AIResponse.model_validate({"narration": narration, "commands": []})

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


def run_soak(
    *,
    turns: int,
    players: int,
    mode: str,
    thread_id: str,
    sample_every: int,
    database_url: str = "",
) -> dict:
    if database_url.strip():
        os.environ["AIGM_DATABASE_URL"] = database_url.strip()
    from aigm.db.bootstrap import ensure_schema
    from aigm.db.session import SessionLocal
    from aigm.services.game_service import GameService

    ensure_schema()
    service = GameService(DeterministicSoakLLM())
    latencies_ms: list[float] = []
    failures = 0
    prompts = [
        "I look around for clues.",
        "I talk to nearby townsfolk.",
        "I inspect my gear.",
        "I move toward the market square.",
        "I ask about recent rumors.",
    ]

    tracemalloc.start()
    mem_initial_mb = tracemalloc.get_traced_memory()[0] / (1024.0 * 1024.0)
    mem_peak_mb = mem_initial_mb

    with SessionLocal() as db:
        campaign = service.get_or_create_campaign(db, thread_id=thread_id, mode=mode)
        service.set_rule(db, campaign, "game_started", "true")

        for i in range(max(1, turns)):
            actor_index = i % max(1, players)
            actor_id = f"soak_player_{actor_index}"
            actor_name = f"SoakPlayer{actor_index}"
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
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            latencies_ms.append(elapsed_ms)
            if sample_every > 0 and ((i + 1) % sample_every == 0):
                current_mb, peak_mb = tracemalloc.get_traced_memory()
                mem_peak_mb = max(mem_peak_mb, peak_mb / (1024.0 * 1024.0))

    current_mb, peak_mb = tracemalloc.get_traced_memory()
    mem_final_mb = current_mb / (1024.0 * 1024.0)
    mem_peak_mb = max(mem_peak_mb, peak_mb / (1024.0 * 1024.0))
    tracemalloc.stop()

    avg = statistics.mean(latencies_ms) if latencies_ms else 0.0
    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    p95 = statistics.quantiles(latencies_ms, n=20)[18] if len(latencies_ms) >= 20 else max(latencies_ms or [0.0])
    p99 = statistics.quantiles(latencies_ms, n=100)[98] if len(latencies_ms) >= 100 else max(latencies_ms or [0.0])
    return {
        "completed_at": f"{datetime.utcnow().isoformat()}Z",
        "turns": int(turns),
        "players": int(players),
        "mode": mode,
        "thread_id": thread_id,
        "failures": int(failures),
        "latency_ms": {
            "avg": round(avg, 3),
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
        },
        "memory_mb": {
            "initial": round(mem_initial_mb, 3),
            "final": round(mem_final_mb, 3),
            "peak": round(mem_peak_mb, 3),
            "growth": round(mem_final_mb - mem_initial_mb, 3),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly soak test for long-running turns and memory growth checks.")
    parser.add_argument("--turns", type=int, default=1500, help="Number of turns to execute.")
    parser.add_argument("--players", type=int, default=8, help="Number of simulated players.")
    parser.add_argument("--thread-id", default=f"nightly-soak-{int(time.time())}", help="Campaign thread id.")
    parser.add_argument("--mode", default="story", choices=["story", "dnd"])
    parser.add_argument("--sample-every", type=int, default=50, help="Memory sample interval in turns.")
    parser.add_argument("--database-url", default="", help="Optional DB URL override (e.g. sqlite:///./aigm_soak.db).")
    parser.add_argument("--max-failures", type=int, default=0, help="Maximum allowed failed turns.")
    parser.add_argument("--max-p95-ms", type=float, default=3500.0, help="Maximum allowed p95 latency in ms.")
    parser.add_argument("--max-memory-growth-mb", type=float, default=256.0, help="Maximum allowed memory growth in MB.")
    args = parser.parse_args()

    summary = run_soak(
        turns=max(1, int(args.turns)),
        players=max(1, int(args.players)),
        mode=args.mode,
        thread_id=args.thread_id,
        sample_every=max(1, int(args.sample_every)),
        database_url=str(args.database_url or ""),
    )

    print("[nightly-soak] summary")
    for line in (
        f"completed_at={summary['completed_at']}",
        f"turns={summary['turns']} players={summary['players']} mode={summary['mode']} thread_id={summary['thread_id']}",
        f"failures={summary['failures']}",
        (
            "latency_ms avg={avg} p50={p50} p95={p95} p99={p99}".format(
                avg=summary["latency_ms"]["avg"],
                p50=summary["latency_ms"]["p50"],
                p95=summary["latency_ms"]["p95"],
                p99=summary["latency_ms"]["p99"],
            )
        ),
        (
            "memory_mb initial={initial} final={final} peak={peak} growth={growth}".format(
                initial=summary["memory_mb"]["initial"],
                final=summary["memory_mb"]["final"],
                peak=summary["memory_mb"]["peak"],
                growth=summary["memory_mb"]["growth"],
            )
        ),
    ):
        print(f"[nightly-soak] {line}")

    ok = True
    if int(summary["failures"]) > int(args.max_failures):
        print(
            f"[nightly-soak][FAIL] failures={summary['failures']} exceeds max-failures={int(args.max_failures)}"
        )
        ok = False
    if float(summary["latency_ms"]["p95"]) > float(args.max_p95_ms):
        print(
            f"[nightly-soak][FAIL] p95={summary['latency_ms']['p95']} exceeds max-p95-ms={float(args.max_p95_ms)}"
        )
        ok = False
    if float(summary["memory_mb"]["growth"]) > float(args.max_memory_growth_mb):
        print(
            "[nightly-soak][FAIL] memory_growth_mb={growth} exceeds max-memory-growth-mb={limit}".format(
                growth=summary["memory_mb"]["growth"],
                limit=float(args.max_memory_growth_mb),
            )
        )
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
