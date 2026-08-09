from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .database import PostgresRepository
from .jobs import BomInputLine, parse_bom
from .mcp_server import build_mcp
from .migrations import current_schema_version
from .procurement import ProcurementService


class Repository(Protocol):
    def create_job(self, source_text: str, lines: list[BomInputLine]) -> int: ...
    def get_job(self, job_id: int) -> dict[str, Any] | None: ...


class _LazyRepository:
    def __getattr__(self, name: str):
        return getattr(PostgresRepository(_database_url()), name)


class JobRequest(BaseModel):
    parts: str


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL fehlt")
    return value


def create_app(
    repository: Repository | None = None,
    schema_version_provider: Callable[[], int] | None = None,
) -> FastAPI:
    active_repository = repository or _LazyRepository()
    procurement = ProcurementService(active_repository)
    mcp = build_mcp(procurement)
    mcp_http_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield

    application = FastAPI(title="Beschaffung", version="0.1.0", lifespan=lifespan)
    application.state.procurement = procurement
    application.state.mcp = mcp

    def get_schema_version() -> int:
        if schema_version_provider is not None:
            return schema_version_provider()
        return current_schema_version(_database_url())

    @application.get("/health")
    def health() -> dict[str, int | str]:
        return {"status": "ok", "schema_version": get_schema_version()}

    @application.post("/api/jobs", status_code=201)
    def create_job(request: JobRequest) -> dict[str, int | str]:
        lines = parse_bom(request.parts)
        if not lines:
            raise HTTPException(422, "Die Liste braucht mindestens eine Position")
        job_id = active_repository.create_job(request.parts, lines)
        return {"job_id": job_id, "status": "offen", "line_count": len(lines)}

    @application.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict[str, Any]:
        job = active_repository.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Job nicht gefunden")
        return job

    application.mount("/", mcp_http_app)
    return application


app = create_app()
