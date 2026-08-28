from __future__ import annotations

import hmac
import io
import json
import os
import secrets
import zipfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .cart import CartError, CartTemporaryError, CartVerificationError
from .database import PostgresRepository
from .mcp_server import build_mcp
from .migrations import current_schema_version
from .procurement import ProcurementService, ValidationError
from .updates import update_available
from .version import __version__


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")
TEMPLATES.env.globals["app_version"] = __version__
TEMPLATES.env.globals["update_release"] = update_available
EXTENSION_DIR = ROOT / "extension"


def relative_time(value: datetime | str) -> str:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    days = max((datetime.now(timezone.utc) - moment).days, 0)
    if days >= 365:
        years = days // 365
        return f"vor {years} {'Jahr' if years == 1 else 'Jahren'}"
    if days >= 30:
        months = days // 30
        return f"vor {months} {'Monat' if months == 1 else 'Monaten'}"
    if days >= 1:
        return f"vor {days} {'Tag' if days == 1 else 'Tagen'}"
    return "heute"


def extension_files() -> list[Path]:
    """Dateien der Extension in stabiler Reihenfolge."""
    return sorted(path for path in EXTENSION_DIR.rglob("*") if path.is_file())


def extension_version() -> str:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest["version"])


def build_extension_zip() -> bytes:
    """Das deployte extension/-Verzeichnis zippen - deterministisch.

    Feste Zeitstempel und sortierte Reihenfolge, damit zweimal Herunterladen
    byte-identisch ist. Gebaut wird aus dem ausgecheckten Stand, deshalb liefert
    jeder Push automatisch den passenden Download, ohne Hook oder CI.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in extension_files():
            info = zipfile.ZipInfo(
                path.relative_to(EXTENSION_DIR).as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


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
    versand_chf: float | None
    gratis_ab_chf: float | None = None
    mindestbestellwert_chf: float | None = None
    lieferzeit_default_tage: int | None = None
    profil_quelle_url: str
    versand_text: str
    waehrung: str = "CHF"


class OfferFetchRequest(BaseModel):
    produkt_url: str


class ManualOfferRequest(BaseModel):
    """Ein abgetipptes Angebot. Die Texte kommen wörtlich von der Seite."""

    produkt_url: str
    produktname: str
    preis: Decimal
    waehrung: str = "CHF"
    lieferzeit_text: str | None = None
    lager_text: str | None = None
    artikelnummer: str | None = None


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
    csrf_secret = secrets.token_bytes(32)
    mcp = build_mcp(procurement)
    mcp_http_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield

    application = FastAPI(title="Beschaffung", version=__version__, lifespan=lifespan)
    application.state.procurement = procurement
    application.state.mcp = mcp
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    def get_schema_version() -> int:
        if schema_version_provider is not None:
            return schema_version_provider()
        return current_schema_version(_database_url())

    def valid_csrf_token(token: str | None) -> bool:
        return bool(
            token
            and 32 <= len(token) <= 128
            and all(character.isalnum() or character in "-_" for character in token)
        )

    def csrf_form_token(cookie_token: str) -> str:
        return hmac.digest(csrf_secret, cookie_token.encode(), "sha256").hex()

    def valid_csrf_form_token(token: str) -> bool:
        return len(token) == 64 and all(
            character in "0123456789abcdef" for character in token
        )

    @application.get("/health")
    def health() -> dict[str, int | str]:
        return {
            "status": "ok",
            "schema_version": get_schema_version(),
            "app_version": __version__,
        }

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
        cookie_token = request.cookies.get("beschaffung_csrf")
        set_csrf_cookie = not valid_csrf_token(cookie_token)
        if set_csrf_cookie:
            cookie_token = secrets.token_urlsafe(32)
        assert cookie_token is not None
        response = TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {"job": job, "csrf_token": csrf_form_token(cookie_token)},
        )
        if set_csrf_cookie:
            response.set_cookie(
                "beschaffung_csrf",
                cookie_token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=8 * 60 * 60,
            )
        return response

    @application.post("/jobs/{job_id}/delete")
    def delete_job_form(
        request: Request,
        job_id: int,
        confirm_job_id: int = Form(...),
        csrf_token: str = Form(""),
    ):
        cookie_token = request.cookies.get("beschaffung_csrf", "")
        if (
            not valid_csrf_token(cookie_token)
            or not valid_csrf_form_token(csrf_token)
            or not hmac.compare_digest(csrf_form_token(cookie_token), csrf_token)
        ):
            raise HTTPException(403, "Ungültige Formularbestätigung")
        try:
            procurement.delete_job(job_id, confirm_job_id)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        return RedirectResponse("/", status_code=303)

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

    def stock_context(error: str | None = None) -> dict[str, Any]:
        stock = [
            {**row, "relative_time": relative_time(row["aktualisiert_am"])}
            for row in procurement.get_stock()
        ]
        movements = [
            {**row, "relative_time": relative_time(row["erstellt_am"])}
            for row in active_repository.get_stock_bewegungen(20)
        ]
        return {"stock": stock, "bewegungen": movements, "error": error}

    @application.get("/bestand", response_class=HTMLResponse)
    def stock_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "bestand.html", stock_context())

    @application.post("/bestand/korrektur")
    def correct_stock_form(
        request: Request,
        stock_id: int = Form(...),
        delta: int = Form(...),
        kommentar: str = Form(...),
    ):
        try:
            procurement.korrigiere_bestand(stock_id, delta, kommentar)
        except ValidationError as error:
            return TEMPLATES.TemplateResponse(
                request,
                "bestand.html",
                stock_context(str(error)),
                status_code=422,
            )
        return RedirectResponse("/bestand", status_code=303)

    @application.get("/shops", response_class=HTMLResponse)
    def shops(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "shops.html",
            {
                "shops": active_repository.list_shops(),
                "lieferziele": procurement.list_lieferziele(),
            },
        )

    @application.post("/lieferziele")
    def create_lieferziel_form(
        name: str = Form(...),
        adresse: str = Form(...),
        land: str = Form(...),
        waehrung: str = Form(""),
        aufschlag_chf: str = Form("0"),
        zuschlag_tage: str = Form("0"),
    ):
        try:
            procurement.record_lieferziel(
                name,
                adresse,
                land,
                waehrung=waehrung or None,
                aufschlag_chf=aufschlag_chf or 0,
                zuschlag_tage=int(zuschlag_tage or 0),
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        return RedirectResponse("/shops", status_code=303)

    @application.post("/lieferziele/{lieferziel_id}")
    def update_lieferziel_form(
        lieferziel_id: int,
        adresse: str = Form(...),
        waehrung: str = Form(...),
        aufschlag_chf: str = Form("0"),
        zuschlag_tage: str = Form("0"),
    ):
        try:
            procurement.update_lieferziel(
                lieferziel_id,
                adresse=adresse,
                waehrung=waehrung,
                aufschlag_chf=aufschlag_chf or 0,
                zuschlag_tage=int(zuschlag_tage or 0),
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        return RedirectResponse("/shops", status_code=303)

    @application.get("/api/lieferziele")
    def list_lieferziele() -> list[dict[str, Any]]:
        return procurement.list_lieferziele()

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
                waehrung=profile.waehrung,
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

    def line_of_job(job_id: int, line_id: int) -> dict[str, Any]:
        """Die Zeile muss zu diesem Job gehören.

        Ohne diese Prüfung liesse sich über die Job-URL ein Angebot an eine
        fremde Zeile hängen - die Zuordnung ist der einzige Teil, den die
        Engine nicht selbst nachprüfen kann.
        """
        job = active_repository.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"Job {job_id} ist unbekannt")
        for line in job.get("lines", []):
            if int(line["id"]) == line_id:
                return line
        raise HTTPException(404, f"Zeile {line_id} gehört nicht zu Job {job_id}")

    @application.post("/api/jobs/{job_id}/lines/{line_id}/offers/fetch")
    def fetch_offer_for_line(
        job_id: int, line_id: int, request: OfferFetchRequest
    ) -> dict[str, Any]:
        line_of_job(job_id, line_id)
        try:
            return procurement.fetch_offer(line_id, request.produkt_url)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/jobs/{job_id}/lines/{line_id}/offers")
    def record_offer_for_line(
        job_id: int, line_id: int, request: ManualOfferRequest
    ) -> dict[str, Any]:
        line_of_job(job_id, line_id)
        try:
            return procurement.record_manual_offer(
                line_id,
                request.produkt_url,
                request.produktname,
                request.preis,
                request.waehrung,
                request.lieferzeit_text,
                request.lager_text,
                request.artikelnummer,
            )
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/offers/{offer_id}/refresh")
    def refresh_offer(offer_id: int) -> dict[str, Any]:
        try:
            return procurement.refresh_offer(offer_id)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.get("/api/jobs/{job_id}/refreshable")
    def refreshable_offers(job_id: int) -> list[dict[str, Any]]:
        try:
            return procurement.refreshable_offers(job_id)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.get("/api/jobs/{job_id}/variants")
    def variants(job_id: int, tempo: float = 0.5) -> list[dict[str, Any]]:
        try:
            return procurement.plan_order(job_id, tempo)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @application.get("/api/extension")
    def extension_info() -> dict[str, Any]:
        return {
            "version": extension_version(),
            "download_url": "/extension.zip",
            "dateien": [
                path.relative_to(EXTENSION_DIR).as_posix()
                for path in extension_files()
            ],
        }

    @application.get("/extension.zip")
    def extension_zip() -> Response:
        payload = build_extension_zip()
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="beschaffung-extension-{extension_version()}.zip"'
                )
            },
        )

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
