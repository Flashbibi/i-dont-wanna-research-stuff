"""Fremdwährung mit Provenienz.

Zwei Regeln tragen dieses Modul:

* **Der Server rechnet, nie der Agent.** Umrechnung ist Arithmetik und damit
  deterministisch - sie gehört in Code, genau wie der Optimierer. Ein Modell
  liefert den Originalpreis, nichts weiter.
* **Kein CHF-Wert ohne belegte Umrechnung.** Zu jedem umgerechneten Preis
  gehören Originalbetrag, Kurs, Kursdatum und Quelle. Dieselbe Disziplin wie
  bei Lieferzeiten, Versandprofilen und Plattformen.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

HOME_CURRENCY = "CHF"

#: Freie, schlüssellose Quelle für EZB-Referenzkurse.
KURS_API = "https://api.frankfurter.app/latest"

#: Ab hier gilt ein Kurs als veraltet und die Oberfläche zeigt ein Badge.
KURS_MAX_ALTER_TAGE = 7

RATE_TIMEOUT = 15

#: Die Quelle blockt anonyme Bibliotheks-Clients; ein benannter Agent ist höflich
#: und funktioniert.
KURS_USER_AGENT = "beschaffung/1.0 (LAN-Beschaffungstool)"


#: Währung folgt dem Land des Lieferziels, ist aber überschreibbar - deshalb
#: eine Ableitung und keine Regel.
LAND_WAEHRUNG = {
    "CH": "CHF",
    "LI": "CHF",
    "DE": "EUR",
    "AT": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "ES": "EUR",
    "GB": "GBP",
    "US": "USD",
}


def waehrung_fuer_land(land: str) -> str | None:
    return LAND_WAEHRUNG.get((land or "").strip().upper())


class KursError(ValueError):
    """Kein Kurs beschaffbar und auch keiner bekannt."""


@dataclass(frozen=True)
class Kurs:
    waehrung: str
    kurs: Decimal
    geholt_am: date
    quelle_url: str
    #: True, wenn der Tagesabruf scheiterte und ein älterer Kurs einspringt.
    ersatzweise: bool = False

    def alter_tage(self, heute: date) -> int:
        return (heute - self.geholt_am).days

    def veraltet(self, heute: date) -> bool:
        return self.alter_tage(heute) > KURS_MAX_ALTER_TAGE

    def beleg(self) -> str:
        """Kurzer Nachweis für die Oberfläche: «Kurs 0.9400 (EZB, 11.08.)»."""
        quelle = "EZB" if "frankfurter" in self.quelle_url else self.quelle_url
        return f"Kurs {self.kurs:.4f} ({quelle}, {self.geholt_am:%d.%m.})"


class KursRepository(Protocol):
    def get_kurs(self, waehrung: str) -> dict[str, Any] | None: ...
    def save_kurs(
        self, waehrung: str, kurs: Decimal, geholt_am: date, quelle_url: str
    ) -> dict[str, Any]: ...


def fetch_kurs(
    waehrung: str, *, opener: Callable[[str], str] | None = None
) -> tuple[Decimal, str]:
    """Tageskurs <waehrung> -> CHF abrufen.

    Liefert Kurs und die konkrete Abruf-URL als Beleg. Netzwerkfehler werden
    nicht geschluckt - der Aufrufer entscheidet, ob ein älterer Kurs einspringt.
    """
    url = f"{KURS_API}?from={waehrung}&to={HOME_CURRENCY}"
    if opener is None:

        def opener(target: str) -> str:
            # httpx statt urllib: die Quelle weist urllibs Default-User-Agent
            # mit HTTP 403 ab. Gemockte Tests sehen das nicht, der erste echte
            # Abruf schon - deshalb hier derselbe Weg wie im Cart-Adapter.
            import httpx

            response = httpx.get(
                target,
                headers={"Accept": "application/json", "User-Agent": KURS_USER_AGENT},
                timeout=RATE_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.text

    payload = json.loads(opener(url))
    rate = payload.get("rates", {}).get(HOME_CURRENCY)
    if rate is None:
        raise KursError(f"Quelle nennt keinen {waehrung}->{HOME_CURRENCY}-Kurs: {url}")
    kurs = Decimal(str(rate))
    if kurs <= 0:
        raise KursError(f"Quelle liefert einen unbrauchbaren Kurs: {kurs}")
    return kurs, url


def aktueller_kurs(
    repository: KursRepository,
    waehrung: str,
    heute: date,
    *,
    opener: Callable[[str], str] | None = None,
) -> Kurs:
    """Kurs für heute besorgen: Cache, sonst Abruf, sonst letzter bekannter.

    Der Abruf passiert bei Erstbedarf am Tag, nicht auf Vorrat. Scheitert er,
    springt der letzte bekannte Kurs ein und wird als ``ersatzweise`` markiert -
    sichtbar in der Oberfläche, nicht still.
    """
    if waehrung == HOME_CURRENCY:
        return Kurs(HOME_CURRENCY, Decimal("1"), heute, "Heimwährung")

    bekannt = repository.get_kurs(waehrung)
    if bekannt is not None:
        gespeichert = _als_kurs(bekannt)
        if gespeichert.geholt_am >= heute:
            return gespeichert
    else:
        gespeichert = None

    try:
        kurs, quelle = fetch_kurs(waehrung, opener=opener)
    except Exception as error:  # noqa: BLE001 - jeder Abrufweg darf scheitern
        if gespeichert is None:
            raise KursError(
                f"Kein {waehrung}-Kurs beschaffbar und keiner gespeichert. "
                "Ohne belegten Kurs wird kein Preis umgerechnet."
            ) from error
        return Kurs(
            gespeichert.waehrung,
            gespeichert.kurs,
            gespeichert.geholt_am,
            gespeichert.quelle_url,
            ersatzweise=True,
        )

    repository.save_kurs(waehrung, kurs, heute, quelle)
    return Kurs(waehrung, kurs, heute, quelle)


def _als_kurs(row: dict[str, Any]) -> Kurs:
    geholt = row["geholt_am"]
    if isinstance(geholt, str):
        geholt = date.fromisoformat(geholt[:10])
    return Kurs(
        str(row["waehrung"]),
        Decimal(str(row["kurs"])),
        geholt,
        str(row["quelle_url"]),
    )


def nach_chf(preis_original: Decimal, kurs: Decimal) -> Decimal:
    """Originalbetrag in CHF umrechnen, kaufmännisch auf Rappen gerundet."""
    return (preis_original * kurs).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def kurs_badge(kurs: Kurs, heute: date) -> str | None:
    """Warnung für die Oberfläche, oder None wenn alles frisch ist."""
    if kurs.ersatzweise:
        return (
            f"Kurs veraltet - Tagesabruf fehlgeschlagen, es gilt der Kurs vom "
            f"{kurs.geholt_am:%d.%m.}"
        )
    if kurs.veraltet(heute):
        return f"Kurs veraltet - {kurs.alter_tage(heute)} Tage alt"
    return None


def ist_veraltet(geholt_am: date, heute: date) -> bool:
    return heute - geholt_am > timedelta(days=KURS_MAX_ALTER_TAGE)
