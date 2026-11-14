from __future__ import annotations

import argparse
import os
from pathlib import Path

from plankton_core import JobStore, RuntimeMode


def set_admin_mode(database_path: Path, action: str) -> RuntimeMode:
    store = JobStore(database_path)
    if not database_path.is_file():
        raise RuntimeError(f"database does not exist: {database_path}")
    if action == "pause":
        mode = RuntimeMode.TRAINING
    elif action == "resume":
        mode = RuntimeMode.INFERENCE_ENABLED
    else:
        raise ValueError(f"unsupported action: {action}")
    store.set_runtime_mode(mode, require_idle=action == "pause")
    return mode


def main() -> None:
    parser = argparse.ArgumentParser(description="Internal workstation runtime administration")
    parser.add_argument("action", choices=("pause", "resume"))
    args = parser.parse_args()
    database_path = Path(
        os.environ.get("PLANKTON_DATABASE_PATH", "/var/lib/plankton-inference/app.db")
    )
    try:
        mode = set_admin_mode(database_path, args.action)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"plankton runtime admin: {error}\n")
    print(f"mode={mode}")


if __name__ == "__main__":
    main()
