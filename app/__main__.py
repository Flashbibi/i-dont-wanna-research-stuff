# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
from __future__ import annotations

import os

import uvicorn

from .migrations import run_migrations


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL fehlt")
    run_migrations(database_url)
    uvicorn.run("app.web:app", host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
