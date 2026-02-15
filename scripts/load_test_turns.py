from __future__ import annotations

import argparse
import statistics
import time
from datetime import datetime

from aigm.adapters.llm import LLMAdapter
from aigm.db.bootstrap import ensure_schema
from aigm.db.session import SessionLocal
from aigm.services.game_service import GameService


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight load test for local turn-processing throughput.")
    parser.add_argument("--turns", type=int, default=100, help="Number of turns to execute.")
    parser.add_argument("--players", type=int, default=4, help="Number of simulated players.")
    parser.add_argument("--thread-id", default=f"loadtest-{int(time.time())}", help="Campaign thread id.")
    parser.add_argument("--mode", default="story", choices=["story", "dnd"])
    args = parser.parse_args()

    ensure_schema()
    service = GameService(LLMAdapter())
    latencies_ms: list[float] = []
    failures = 0

    with SessionLocal() as db:
        campaign = service.get_or_create_campaign(db, thread_id=args.thread_id, mode=args.mode)
        prompts = [
            "I look around for clues.",
            "I speak with nearby townsfolk.",
            "I inspect my inventory and prepare.",
            "I move toward the market square.",
            "I ask if anyone saw suspicious activity.",
        ]

        for i in range(max(1, args.turns)):
            actor_index = i % max(1, args.players)
            actor_id = f"load_player_{actor_index}"
            actor_name = f"LoadPlayer{actor_index}"
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

    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    p95 = statistics.quantiles(latencies_ms, n=20)[18] if len(latencies_ms) >= 20 else max(latencies_ms or [0.0])
    p99 = statistics.quantiles(latencies_ms, n=100)[98] if len(latencies_ms) >= 100 else max(latencies_ms or [0.0])
    avg = statistics.mean(latencies_ms) if latencies_ms else 0.0
    print(f"[load-test] completed_at={datetime.utcnow().isoformat()}Z")
    print(f"[load-test] turns={args.turns} players={args.players} mode={args.mode} thread_id={args.thread_id}")
    print(f"[load-test] failures={failures}")
    print(f"[load-test] latency_ms avg={avg:.2f} p50={p50:.2f} p95={p95:.2f} p99={p99:.2f}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

