from __future__ import annotations

import os

import uvicorn

from .app import ApiSettings, create_app


def main() -> None:
    os.umask(0o007)
    uvicorn.run(
        create_app(ApiSettings.from_environment()),
        host="127.0.0.1",
        port=int(os.environ.get("PLANKTON_API_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
