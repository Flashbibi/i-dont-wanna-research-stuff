from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .cart import CartError, CartTemporaryError, CartVerificationError
from .database import PostgresRepository
from .mcp_server import build_mcp
from .migrations import current_schema_version
from .procurement import ProcurementService, ValidationError


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")


class _LazyRepository:
    def __getattr__(self, name: str):
        return getattr(PostgresRepository(_database_url()), name)


class JobRequest(BaseModel):
    parts: str


class DecisionRequest(BaseModel):
    status: Literal["pin", "exclude", "neutral"]


class ScenarioRequest(BaseModel):
    pins: dict[str, int] | None = None
    excludes: list[int] | None = None
    tempo: float = 0.5


class PlanSelectionRequest(BaseModel):
    assignments: dict[str, int]
    tempo: float = 0.5


class PlanDeltaRequest(BaseModel):
    offer_id: int
    base_assignments: dict[str, int]
    tempo: float = 0.5


class ShopProfileRequest(BaseModel):
    versand_chf: float
    gratis_ab_chf: float | None = None
    mindestbestellwert_chf: float | None = None
    lieferzeit_default_tage: int | None = None
    profil_quelle_url: str
    versand_text: str


class PurchaseRequest(BaseModel):
    variante: dict[str, Any]
    zugesagt_liefertage_pro_shop: dict[str, int]


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL fehlt")
    return value


def create_app(
    repository: Any | None = None,
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
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    def get_schema_version() -> int:
        if schema_version_provider is not None:
            return schema_version_provider()
        return current_schema_version(_database_url())

    @application.get("/health")
    def health() -> dict[str, int | str]:
        return {"status": "ok", "schema_version": get_schema_version()}

    @application.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return TEMPLATES.TemplateResponse(
            request, "home.html", {"jobs": active_repository.list_jobs()}
        )

    @application.post("/jobs")
    def create_job_form(parts: str = Form(...)):
        try:
            result = procurement.create_job(parts)
        except ValidationError as error:
            raise HTTPException(422, str(error)) from error
        return RedirectResponse(f"/jobs/{result['job_id']}", status_code=303)

    @application.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        job = active_repository.get_job_detail(job_id)
        if job is None:
            raise HTTPException(404, "Job nicht gefunden")
        return TEMPLATES.TemplateResponse(request, "job.html", {"job": job})

    @application.get("/jobs/{job_id}/variants", response_class=HTMLResponse)
    def variants_page(request: Request, job_id: int):
        if active_repository.get_job(job_id) is None:
            raise HTTPException(404, "Job nicht gefunden")
        return TEMPLATES.TemplateResponse(
            request, "variants.html", {"job_id": job_id}
        )

    @application.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        return TEMPLATES.TemplateResponse(
            request, "history.html", {"purchases": active_repository.list_purchases()}
        )

    @application.post("/purchases/{purchase_id}/repeat")
    def repeat_purchase(purchase_id: int):
        job_id = active_repository.repeat_purchase(purchase_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @application.post("/purchases/{purchase_id}/arrived")
    def purchase_arrived(purchase_id: int):
        active_repository.mark_purchase_arrived(purchase_id)
        return RedirectResponse("/history", status_code=303)

    @application.get("/shops", response_class=HTMLResponse)
    def shops(request: Request):
        return TEMPLATES.TemplateResponse(
            request, "shops.html", {"shops": active_repository.list_shops()}
        )

    @application.post("/shops/{shop_id}/status")
    def update_shop(shop_id: int, status: str = Form(...)):
        active_repository.update_shop_status(shop_id, status)
        return RedirectResponse("/shops", status_code=303)

    @application.put("/api/shops/{shop_id}/profile")
    def update_shop_profile(shop_id: int, profile: ShopProfileRequest) -> dict[str, Any]:
        try:
            return procurement.record_shop_profile(
                shop_id,
                versand_chf=profile.versand_chf,
                gratis_ab_chf=profile.gratis_ab_chf,
                mindestbestellwert_chf=profile.mindestbestellwert_chf,
                lieferzeit_default_tage=profile.lieferzeit_default_tage,
                profil_quelle_url=profile.profil_quelle_url,
                versand_text=profile.versand_text,
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/jobs", status_code=201)
    def create_job(request: JobRequest) -> dict[str, Any]:
        try:
            result = procurement.create_job(request.parts)
        except ValidationError as error:
            raise HTTPException(422, str(error)) from error
        return {
            "job_id": result["job_id"],
            "status": "offen",
            "line_count": len(result["lines"]),
        }

    def require_e2e_marker(marker: str | None) -> None:
        if marker != "beschaffung-e2e-disposable":
            raise HTTPException(404, "Nicht gefunden")

    @application.post("/api/e2e/jobs", status_code=201)
    def create_e2e_job(x_e2e_marker: str | None = Header(default=None)) -> dict[str, Any]:
        require_e2e_marker(x_e2e_marker)
        try:
            return active_repository.create_e2e_test_job()
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @application.delete("/api/e2e/jobs/{job_id}")
    def delete_e2e_job(
        job_id: int,
        x_e2e_marker: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_e2e_marker(x_e2e_marker)
        try:
            return active_repository.delete_e2e_test_job(job_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @application.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict[str, Any]:
        job = active_repository.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Job nicht gefunden")
        return job

    @application.post("/offers/{offer_id}/decision")
    def decide_offer_form(offer_id: int, status: str = Form(...), job_id: int = Form(...)):
        if status not in {"pin", "exclude", "neutral"}:
            raise HTTPException(422, "Ungültige Entscheidung")
        try:
            active_repository.record_decision(offer_id, status)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return RedirectResponse(f"/jobs/{job_id}#offer-{offer_id}", status_code=303)

    @application.post("/api/offers/{offer_id}/decision")
    def decide_offer(offer_id: int, request: DecisionRequest) -> dict[str, Any]:
        try:
            return active_repository.record_decision(offer_id, request.status)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/jobs/{job_id}/scenarios")
    def scenarios(job_id: int, request: ScenarioRequest) -> dict[str, Any]:
        try:
            return procurement.plan_scenarios(
                job_id,
                pins=request.pins,
                excludes=request.excludes,
                tempo=request.tempo,
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.put("/api/jobs/{job_id}/selection")
    def select_plan(job_id: int, request: PlanSelectionRequest) -> dict[str, Any]:
        try:
            return procurement.select_plan(
                job_id, request.assignments, tempo=request.tempo
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/jobs/{job_id}/lines/{line_id}/delta")
    def plan_delta(
        job_id: int, line_id: int, request: PlanDeltaRequest
    ) -> dict[str, Any]:
        try:
            return procurement.plan_delta(
                job_id,
                line_id=line_id,
                offer_id=request.offer_id,
                base_assignments=request.base_assignments,
                tempo=request.tempo,
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.get("/api/jobs/{job_id}/variants")
    def variants(job_id: int, tempo: float = 0.5) -> list[dict[str, Any]]:
        try:
            return procurement.plan_order(job_id, tempo)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.get("/api/jobs/{job_id}/cart-shops")
    def cart_shops(job_id: int) -> list[dict[str, Any]]:
        try:
            return procurement.cart_shops(job_id)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/jobs/{job_id}/shops/{shop_id}/cart")
    def fill_cart(
        job_id: int,
        shop_id: int,
        stub: str | None = None,
        x_e2e_marker: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Gast-Warenkorb füllen und nach Rückverifikation übergeben.

        Die Statuscodes trennen die Ausgänge, die die Oberfläche unterschiedlich
        darstellen muss: 503 ist wiederholbar, 409 ist ein belegter Unterschied
        zwischen Erfassung und Korb.

        ``stub`` ist ausschliesslich für den E2E-Klickpfad und nur mit gültigem
        Marker erreichbar, damit kein Aufruf von aussen einen erfundenen Korb
        bestätigt bekommt.
        """
        if stub is not None:
            require_e2e_marker(x_e2e_marker)
            if stub not in {"ok", "mismatch"}:
                raise HTTPException(422, "stub muss 'ok' oder 'mismatch' sein")
            # Der Marker allein genügt nicht: ein gestubtes "Korb geprüft ✓"
            # darf auf einem echten Job nicht existieren können.
            if not active_repository.is_test_job(job_id):
                raise HTTPException(404, "Nicht gefunden")
        try:
            return procurement.fill_cart(job_id, shop_id, stub=stub)
        except CartTemporaryError as error:
            raise HTTPException(503, str(error)) from error
        except CartVerificationError as error:
            raise HTTPException(409, str(error)) from error
        except CartError as error:
            raise HTTPException(422, str(error)) from error
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/jobs/{job_id}/purchase")
    def purchase(job_id: int, request: PurchaseRequest) -> dict[str, Any]:
        try:
            return procurement.record_purchase(
                job_id,
                request.variante,
                datetime.now(timezone.utc).isoformat(),
                request.zugesagt_liefertage_pro_shop,
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    application.mount("/", mcp_http_app)
    return application


app = create_app()
