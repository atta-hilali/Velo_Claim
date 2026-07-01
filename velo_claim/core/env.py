from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    configured = os.getenv("VELO_CLAIM_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path).expanduser()
    if not env_path.is_absolute():
        env_path = project_root() / env_path
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
