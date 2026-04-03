from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from aigm.config import settings
from aigm.ops.supervisor import HealthState, UnifiedLogger, make_health_handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI GameMaster health API as a standalone service.")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=settings.healthcheck_port)
    parser.add_argument("--log-dir", default=settings.log_dir)
    parser.add_argument("--streamlit-url", default=f"http://127.0.0.1:{settings.streamlit_port}")
    args = parser.parse_args()

    logger = UnifiedLogger(Path(args.log_dir))
    state = HealthState(
        streamlit_url=str(args.streamlit_url).rstrip("/"),
        ollama_url=settings.ollama_url,
        logger=logger,
    )
    server = ThreadingHTTPServer((args.bind, int(args.port)), make_health_handler(state))
    print(f"[health_api] listening on http://{args.bind}:{args.port}/health", flush=True)
    try:
        server.serve_forever()
    finally:
        logger.flush(force=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
