from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aigm.config import settings


class ComponentStore:
    """Small local key-value store for component-private runtime config."""

    def __init__(self, component_name: str) -> None:
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in component_name).strip("_") or "component"
        base = Path(settings.component_state_dir)
        self.path = base / safe / "config.json"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self.read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        data = self.read()
        data[key] = value
        self.write(data)
