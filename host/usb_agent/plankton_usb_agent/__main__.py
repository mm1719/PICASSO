from __future__ import annotations

import os
from pathlib import Path

from .agent import UsbAgent
from .server import serve


def main() -> None:
    agent = UsbAgent(
        job_root=Path(os.environ.get("PLANKTON_JOB_ROOT", "/var/lib/plankton-inference/jobs")),
        signature_directory=Path(
            os.environ.get("PLANKTON_CLAMAV_SIGNATURE_DIR", "/var/lib/clamav")
        ),
        max_signature_age_hours=int(
            os.environ.get("PLANKTON_CLAMAV_MAX_SIGNATURE_AGE_HOURS", "72")
        ),
    )
    serve(
        Path(os.environ.get("PLANKTON_USB_AGENT_SOCKET", "/run/plankton-inference/usb-agent.sock")),
        agent,
    )


if __name__ == "__main__":
    main()
