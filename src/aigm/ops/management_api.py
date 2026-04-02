from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from aigm.config import settings
from aigm.ops.supervisor import HealthState, ManagementState, UnifiedLogger, make_management_handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI GameMaster management API as a standalone service.")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=settings.management_api_port)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--log-dir", default=settings.log_dir)
    parser.add_argument("--streamlit-port", type=int, default=settings.streamlit_port)
    parser.add_argument("--health-port", type=int, default=settings.healthcheck_port)
    parser.add_argument("--streamlit-url", default=f"http://127.0.0.1:{settings.streamlit_port}")
    parser.add_argument("--db-api-url", default=settings.db_api_url)
    args = parser.parse_args()

    logger = UnifiedLogger(Path(args.log_dir))
    health_state = HealthState(
        streamlit_url=str(args.streamlit_url).rstrip("/"),
        ollama_url=settings.ollama_url,
        logger=logger,
    )
    state = ManagementState(
        logger=logger,
        health_state=health_state,
        env_path=Path(args.cwd) / ".env",
        streamlit_port=int(args.streamlit_port),
        health_port=int(args.health_port),
        db_api_url=str(args.db_api_url).rstrip("/"),
    )
    server = ThreadingHTTPServer((args.bind, int(args.port)), make_management_handler(state))
    print(f"[management_api] listening on http://{args.bind}:{args.port}/api/v1/meta", flush=True)
    try:
        server.serve_forever()
    finally:
        logger.flush(force=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
