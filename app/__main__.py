from __future__ import annotations

import os
import signal

from .migrations import run_migrations


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL fehlt")
    run_migrations(database_url)
    signal.pause()


if __name__ == "__main__":
    main()
