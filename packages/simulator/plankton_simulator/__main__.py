from __future__ import annotations

import argparse
import os
from pathlib import Path

from .runtime import initialize_fixture, load_scenario, serve_simulator


def _path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return Path(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plankton sealed-runtime simulator")
    parser.add_argument("command", choices=("check", "init", "serve"))
    args = parser.parse_args()
    scenario = _path("PLANKTON_SIMULATOR_CONFIG")
    load_scenario(scenario)
    if args.command == "check":
        return
    usb_root = _path("PLANKTON_SIMULATOR_USB_ROOT")
    if args.command == "init":
        initialize_fixture(usb_root)
        return
    serve_simulator(
        _path("PLANKTON_USB_AGENT_SOCKET"),
        usb_root,
        _path("PLANKTON_JOB_ROOT"),
        scenario,
        _path("PLANKTON_SIMULATOR_JOURNAL"),
    )


if __name__ == "__main__":
    main()
