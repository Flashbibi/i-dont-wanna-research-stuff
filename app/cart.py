"""Warenkorb-Übergabe: deterministische Adapter pro Shop-Plattform.

Reines HTTP, kein Browser und kein Modell - Warenkorb-Füllen ist so
deterministisch wie der Optimierer und gehört deshalb in Code.

Grenzen, die hier bewusst hart sind:

* Nur Gast-Sessions. Keine Logins, keine Kontoerstellung, kein Kaufabschluss.
* Session-Cookies werden nie geloggt und tauchen in keiner Fehlermeldung auf.
* Eine Session pro Füllvorgang, wenige Requests, normale Browser-Header.
* Nach dem Füllen wird der Korb zurückgelesen und gegen die Szenario-Zuordnung
  geprüft. Ohne exakte Übereinstimmung wird nichts übergeben.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol
from urllib.parse import urljoin, urlparse


PLATFORM_OPENCART = "opencart"
PLATFORM_WOOCOMMERCE = "woocommerce"
PLATFORM_SHOPIFY = "shopify"

KNOWN_PLATFORMS = (PLATFORM_OPENCART, PLATFORM_WOOCOMMERCE, PLATFORM_SHOPIFY)

#: Plattformen, für die es einen ausgeführten Adapter gibt. Alles andere fällt
#: auf die bestehende Linkliste zurück.
SUPPORTED_PLATFORMS = (PLATFORM_OPENCART,)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9",
}


class CartError(ValueError):
    """Klartext-Fehler; die Meldung ist für die Oberfläche bestimmt."""


class CartUnsupported(CartError):
    """Für diese Plattform gibt es keinen Adapter; Linkliste bleibt."""


class CartVerificationError(CartError):
    """Der zurückgelesene Korb weicht von der Szenario-Zuordnung ab."""

    def __init__(self, abweichungen: list[str]):
        self.abweichungen = list(abweichungen)
        super().__init__(" ".join(self.abweichungen))


@dataclass(frozen=True)
class CartItem:
    """Eine Position der persistierten Szenarioauswahl für genau einen Shop."""

    line_id: int
    offer_id: int
    produktname: str
    produkt_url: str
    menge: int
    einzelpreis_chf: Decimal
    shop_produkt_id: str | None = None

    @property
    def positionspreis_chf(self) -> Decimal:
        return self.einzelpreis_chf * self.menge


@dataclass(frozen=True)
class PlatformEvidence:
    plattform: str
    beleg: str


@dataclass
class CartFill:
    """Ergebnis eines Füllvorgangs. Enthält nie mehr als nötig."""

    plattform: str
    verifiziert: bool
    artikel_anzahl: int
    total_chf: Decimal
    positionen: list[dict[str, Any]] = field(default_factory=list)
    cookie_name: str | None = None
    cookie_wert: str | None = None
    cart_url: str | None = None
    plattform_beleg: str | None = None
    #: offer_id -> shopinterne Produkt-ID, zum Cachen durch den Aufrufer.
    produkt_ids: dict[int, str] = field(default_factory=dict)


class Response(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class Session(Protocol):
    """Minimale HTTP-Sitzung. In Tests durch eine Attrappe ersetzt."""

    @property
    def cookies(self) -> Mapping[str, str]: ...

    def get(self, url: str) -> Response: ...

    def post(self, url: str, data: Mapping[str, Any]) -> Response: ...


# ---------------------------------------------------------------------------
# Betragsformate
# ---------------------------------------------------------------------------

# Muss mit einer Ziffer beginnen und enden, sonst verschluckt der Ausdruck den
# Punkt in "Fr." oder einen Satzpunkt hinter dem Betrag.
_AMOUNT = re.compile(r"-?\d(?:[\d'’.,]*\d)?")


def parse_chf(text: str | None) -> Decimal:
    """Betrag aus Shop-Markup lesen.

    Deckt die real vorkommenden Schreibweisen ab: ``CHF 5.90``, ``CHF5,90``
    und ``CHF 1'234.55``. Der Apostroph ist Tausendertrennung, das *letzte*
    Komma oder der letzte Punkt trennt die Rappen.
    """
    if text is None:
        raise CartError("Betrag fehlt im Shop-Markup")
    match = _AMOUNT.search(text.replace("\xa0", " "))
    if match is None:
        raise CartError(f"Kein Betrag lesbar aus {text.strip()!r}")
    raw = match.group(0).replace("'", "").replace("’", "")
    last_dot = raw.rfind(".")
    last_comma = raw.rfind(",")
    separator = max(last_dot, last_comma)
    if separator == -1:
        normalized = raw
    else:
        # Alles vor dem Dezimaltrenner ist Gruppierung und fliegt raus.
        head = raw[:separator].replace(".", "").replace(",", "")
        normalized = f"{head}.{raw[separator + 1:]}"
    try:
        return Decimal(normalized)
    except InvalidOperation as error:
        raise CartError(f"Kein Betrag lesbar aus {text.strip()!r}") from error


def format_chf(value: Decimal) -> str:
    return f"CHF {value.quantize(Decimal('0.01'))}"


# ---------------------------------------------------------------------------
# Plattform-Erkennung
# ---------------------------------------------------------------------------

def detect_platform(html: str, cookie_names: list[str]) -> PlatformEvidence | None:
    """Plattform on demand erkennen und einen kurzen Nachweis mitliefern.

    Kein Wert ohne Beleg - dieselbe Disziplin wie bei Lieferzeiten und
    Versandprofilen. Ohne belastbaren Treffer wird ``None`` zurückgegeben,
    nicht geraten.
    """
    names = {name.upper() for name in cookie_names}
    markup = html or ""

    hints: list[str] = []
    if "OCSESSID" in names:
        hints.append("Cookie OCSESSID")
    route_hits = markup.count("index.php?route=")
    if route_hits:
        hints.append(f"index.php?route= im Markup ({route_hits}x)")
    if "catalog/view/theme" in markup:
        hints.append("catalog/view/theme")
    if hints:
        return PlatformEvidence(PLATFORM_OPENCART, "; ".join(hints))

    if "Shopify.theme" in markup or "cdn.shopify.com" in markup:
        beleg = "Shopify.theme" if "Shopify.theme" in markup else "cdn.shopify.com"
        return PlatformEvidence(PLATFORM_SHOPIFY, beleg)

    if "/wp-content/plugins/woocommerce" in markup or "woocommerce-page" in markup:
        beleg = (
            "/wp-content/plugins/woocommerce"
            if "/wp-content/plugins/woocommerce" in markup
            else "woocommerce-page"
        )
        return PlatformEvidence(PLATFORM_WOOCOMMERCE, beleg)

    return None


# ---------------------------------------------------------------------------
# OpenCart
# ---------------------------------------------------------------------------

_INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_TABLE_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_ANCHOR_HREF = re.compile(r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_QUANTITY_NAME = re.compile(r"^quantity\[(.+)\]$", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SUBTOTAL_LABEL = re.compile(r"zwischensumme|sub-?total", re.IGNORECASE)


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf"\b{name}\s*=\s*[\"']([^\"']*)[\"']", tag, re.IGNORECASE)
    return match.group(1) if match else None


def _text(fragment: str) -> str:
    return " ".join(_TAGS.sub(" ", fragment).split())


def extract_opencart_product_id(html: str, produkt_url: str) -> str:
    """``product_id`` aus der Produktseite lesen.

    Quelle ist die Produktseite selbst. Es muss genau einen Kandidaten geben:
    OpenCart rendert die ID einmal im Add-to-Cart-Formular, während verwandte
    Produkte ihre IDs nur in ``wishlist.add(...)``/``compare.add(...)`` tragen.
    Bei null oder mehreren Treffern wird abgebrochen statt geraten.
    """
    found: set[str] = set()
    for tag in _INPUT_TAG.findall(html or ""):
        if (_attr(tag, "name") or "").strip().lower() != "product_id":
            continue
        value = (_attr(tag, "value") or "").strip()
        if value.isdigit():
            found.add(value)
    if len(found) == 1:
        return found.pop()
    if not found:
        raise CartError(
            "Auf der Produktseite steht keine product_id im Warenkorb-Formular "
            f"({produkt_url}). Ohne belegte ID wird nichts in den Korb gelegt."
        )
    raise CartError(
        f"Die Produktseite nennt {len(found)} verschiedene product_id-Werte "
        f"({', '.join(sorted(found))}) - {produkt_url}. "
        "Mehrdeutig, deshalb kein Rateversuch."
    )


@dataclass(frozen=True)
class CartEntry:
    href: str | None
    name: str
    menge: int
    zeilensumme_chf: Decimal


def parse_opencart_cart(html: str) -> tuple[list[CartEntry], Decimal]:
    """Korbseite zurücklesen: Positionen und Zwischensumme."""
    entries: list[CartEntry] = []
    for row in _TABLE_ROW.findall(html or ""):
        quantity: int | None = None
        for tag in _INPUT_TAG.findall(row):
            name = (_attr(tag, "name") or "").strip()
            if _QUANTITY_NAME.match(name):
                raw = (_attr(tag, "value") or "").strip()
                if raw.isdigit():
                    quantity = int(raw)
                break
        if quantity is None:
            continue
        cells = _CELL.findall(row)
        amounts = [
            parse_chf(_text(cell))
            for cell in cells
            if re.search(r"\d", _text(cell)) and re.search(r"chf|fr\.", _text(cell), re.I)
        ]
        href_match = _ANCHOR_HREF.search(row)
        name_cells = [_text(cell) for cell in cells if _text(cell) and not re.search(r"chf|fr\.", _text(cell), re.I)]
        entries.append(
            CartEntry(
                href=href_match.group(1) if href_match else None,
                name=name_cells[0] if name_cells else "",
                menge=quantity,
                zeilensumme_chf=amounts[-1] if amounts else Decimal("0.00"),
            )
        )

    subtotal: Decimal | None = None
    for row in _TABLE_ROW.findall(html or ""):
        cells = _CELL.findall(row)
        if len(cells) < 2:
            continue
        if _SUBTOTAL_LABEL.search(_text(cells[-2])):
            subtotal = parse_chf(_text(cells[-1]))
            break
    if subtotal is None:
        raise CartError(
            "Die Korbseite nennt keine Zwischensumme - ohne sie ist keine "
            "Rückverifikation möglich, deshalb keine Übergabe."
        )
    return entries, subtotal


def _same_product(href: str | None, base_url: str, produkt_url: str) -> bool:
    if not href:
        return False
    absolute = urljoin(base_url, href)
    return urlparse(absolute).path.rstrip("/") == urlparse(produkt_url).path.rstrip("/")


class OpenCartAdapter:
    """Gast-Warenkorb bei einem OpenCart-3-Shop füllen und zurücklesen."""

    plattform = PLATFORM_OPENCART
    cookie_name = "OCSESSID"

    def __init__(self, session: Session):
        self.session = session

    # -- Endpunkte ---------------------------------------------------------
    @staticmethod
    def _add_url(base_url: str) -> str:
        return urljoin(base_url, "index.php?route=checkout/cart/add")

    @staticmethod
    def _cart_url(base_url: str) -> str:
        return urljoin(base_url, "index.php?route=checkout/cart")

    # -- Ablauf ------------------------------------------------------------
    def fill(self, base_url: str, items: list[CartItem]) -> CartFill:
        if not items:
            raise CartError("Für diesen Shop enthält der gewählte Plan keine Position")

        landing = self.session.get(base_url)
        evidence = detect_platform(landing.text, list(self.session.cookies))
        if evidence is None or evidence.plattform != PLATFORM_OPENCART:
            raise CartUnsupported(
                "Der Shop antwortet nicht wie OpenCart; es wird kein Korb gefüllt."
            )

        produkt_ids: dict[int, str] = {}
        for item in items:
            product_id = item.shop_produkt_id
            if not product_id:
                page = self.session.get(item.produkt_url)
                if page.status_code != 200:
                    raise CartError(
                        f"Produktseite nicht erreichbar (HTTP {page.status_code}): "
                        f"{item.produkt_url}"
                    )
                product_id = extract_opencart_product_id(page.text, item.produkt_url)
                produkt_ids[item.offer_id] = product_id
            self._add(base_url, item, product_id)

        cart_page = self.session.get(self._cart_url(base_url))
        entries, subtotal = parse_opencart_cart(cart_page.text)
        self._verify(base_url, items, entries, subtotal)

        return CartFill(
            plattform=PLATFORM_OPENCART,
            plattform_beleg=evidence.beleg,
            verifiziert=True,
            artikel_anzahl=sum(entry.menge for entry in entries),
            total_chf=subtotal,
            positionen=[
                {
                    "line_id": item.line_id,
                    "offer_id": item.offer_id,
                    "produktname": item.produktname,
                    "produkt_url": item.produkt_url,
                    "menge": item.menge,
                    "einzelpreis_chf": str(item.einzelpreis_chf),
                    "positionspreis_chf": str(item.positionspreis_chf),
                }
                for item in items
            ],
            cookie_name=self.cookie_name,
            cookie_wert=self.session.cookies.get(self.cookie_name),
            cart_url=self._cart_url(base_url),
            produkt_ids=produkt_ids,
        )

    def _add(self, base_url: str, item: CartItem, product_id: str) -> None:
        response = self.session.post(
            self._add_url(base_url),
            {"product_id": product_id, "quantity": item.menge},
        )
        if response.status_code != 200:
            raise CartError(
                f"Shop lehnt das Hinzufügen ab (HTTP {response.status_code}): "
                f"{item.produktname}"
            )
        try:
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - Shop antwortet nicht wie erwartet
            raise CartError(
                f"Shop antwortet beim Hinzufügen nicht in JSON: {item.produktname}"
            ) from error
        if not isinstance(payload, dict):
            raise CartError(
                f"Shop antwortet beim Hinzufügen unerwartet: {item.produktname}"
            )
        error_payload = payload.get("error")
        if error_payload:
            raise CartError(self._add_error_text(item, error_payload))
        if not payload.get("success"):
            raise CartError(
                f"Shop bestätigt das Hinzufügen nicht: {item.produktname}"
            )

    @staticmethod
    def _add_error_text(item: CartItem, error_payload: Any) -> str:
        if isinstance(error_payload, dict):
            option_errors = [
                str(value)
                for key, value in error_payload.items()
                if str(key).startswith("option")
            ]
            if option_errors:
                return (
                    f"{item.produktname} verlangt eine Auswahl im Shop "
                    f"({'; '.join(option_errors)}). Solche Produkte lassen sich "
                    "nicht blind in den Korb legen - bitte über den Link bestellen."
                )
            joined = "; ".join(str(value) for value in error_payload.values())
            return f"Shop meldet beim Hinzufügen von {item.produktname}: {joined}"
        return f"Shop meldet beim Hinzufügen von {item.produktname}: {error_payload}"

    @staticmethod
    def _verify(
        base_url: str,
        items: list[CartItem],
        entries: list[CartEntry],
        subtotal: Decimal,
    ) -> None:
        """Artikelzahl und Zwischensumme gegen die Zuordnung prüfen.

        Fängt zwei reale Fälle ab: der Shop-Preis hat sich seit ``gesehen_am``
        geändert, und Produkte mit Pflichtoptionen landen nicht wie erwartet
        im Korb.
        """
        abweichungen: list[str] = []

        for item in items:
            matching = [
                entry
                for entry in entries
                if _same_product(entry.href, base_url, item.produkt_url)
            ]
            if not matching:
                abweichungen.append(f"Position fehlt im Korb: {item.produktname}.")
                continue
            menge = sum(entry.menge for entry in matching)
            if menge != item.menge:
                abweichungen.append(
                    f"{item.produktname}: erfasst {item.menge} Stück, "
                    f"Korb {menge} Stück."
                )

        erwartet_artikel = sum(item.menge for item in items)
        korb_artikel = sum(entry.menge for entry in entries)
        if korb_artikel != erwartet_artikel:
            abweichungen.append(
                f"Artikelzahl weicht ab: erfasst {erwartet_artikel}, "
                f"Korb {korb_artikel}."
            )

        erwartet_summe = sum(
            (item.positionspreis_chf for item in items), Decimal("0.00")
        ).quantize(Decimal("0.01"))
        if subtotal.quantize(Decimal("0.01")) != erwartet_summe:
            abweichungen.append(
                f"Zwischensumme weicht ab: erfasst {format_chf(erwartet_summe)}, "
                f"Korb {format_chf(subtotal)}."
            )

        if abweichungen:
            raise CartVerificationError(abweichungen)


def build_adapter(plattform: str | None, session: Session) -> OpenCartAdapter:
    """Adapter zur Plattform wählen.

    ``woocommerce`` und ``shopify`` brauchen keine Session-Übergabe - dort füllt
    eine Cart-URL den Korb direkt im Browser. Das Interface ist vorbereitet,
    aber bewusst nicht ausgeführt: derzeit hat kein erfasster Shop diese
    Plattform, und auf Vorrat wird nichts gebaut.
    """
    if plattform == PLATFORM_OPENCART:
        return OpenCartAdapter(session)
    if plattform in (PLATFORM_WOOCOMMERCE, PLATFORM_SHOPIFY):
        raise CartUnsupported(
            f"Für {plattform} ist noch kein Adapter ausgeführt; "
            "die Bestellliste bleibt der Weg."
        )
    raise CartUnsupported(
        "Plattform des Shops ist unbekannt; die Bestellliste bleibt der Weg."
    )


def open_session() -> Session:
    """Echte HTTP-Sitzung mit normalen Browser-Headern."""
    import httpx

    class _HttpxSession:
        def __init__(self) -> None:
            self._client = httpx.Client(
                headers=BROWSER_HEADERS,
                timeout=25.0,
                follow_redirects=True,
            )

        @property
        def cookies(self) -> Mapping[str, str]:
            return {cookie.name: cookie.value for cookie in self._client.cookies.jar}

        def get(self, url: str):
            return self._client.get(url)

        def post(self, url: str, data: Mapping[str, Any]):
            return self._client.post(
                url, data=dict(data), headers={"X-Requested-With": "XMLHttpRequest"}
            )

        def close(self) -> None:
            self._client.close()

    return _HttpxSession()
