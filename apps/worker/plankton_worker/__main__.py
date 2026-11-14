from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
from threading import Event

from plankton_control import UnixUsbClient

from .worker import Worker, WorkerSettings


def main() -> None:
    os.umask(0o007)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    job_root = Path(os.environ.get("PLANKTON_JOB_ROOT", "/var/lib/plankton-inference/jobs"))
    model_factory = None
    fault_injector = None
    if os.environ.get("PLANKTON_SIMULATOR_CONFIG"):
        from plankton_simulator import simulated_fault_injector, simulated_model_factory

        model_factory = simulated_model_factory
        fault_injector = simulated_fault_injector
    settings = WorkerSettings(
        database_path=Path(
            os.environ.get("PLANKTON_DATABASE_PATH", "/var/lib/plankton-inference/app.db")
        ),
        job_root=job_root,
        gpu_lock_path=Path(
            os.environ.get("PLANKTON_GPU_LOCK_PATH", "/run/plankton-inference/gpu.lock")
        ),
        max_image_bytes=int(os.environ.get("PLANKTON_MAX_IMAGE_BYTES", str(512 * 1024**2))),
        max_image_pixels=int(os.environ.get("PLANKTON_MAX_IMAGE_PIXELS", "250000000")),
        decode_timeout_seconds=int(os.environ.get("PLANKTON_DECODE_TIMEOUT_SECONDS", "30")),
    )
    usb = UnixUsbClient(
        Path(
            os.environ.get(
                "PLANKTON_USB_AGENT_SOCKET", "/run/plankton-inference/usb-agent.sock"
            )
        )
    )
    worker = (
        Worker(settings, usb, model_factory, fault_injector)
        if model_factory is not None and fault_injector is not None
        else Worker(settings, usb)
    )
    stop_event = Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    worker.run_forever(stop_event=stop_event)


if __name__ == "__main__":
    main()
