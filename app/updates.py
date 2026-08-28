"""Update-Check gegen die Releases-API von GitHub, der im Seitenaufbau hängt und ihn nie
aufhalten darf - jeder Fehler endet still als "kein Banner"."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import NamedTuple

from .version import __version__

RELEASE_API = (
    "https://api.github.com/repos/Flashbibi/i-dont-wanna-research-stuff/releases/latest"
)
RELEASE_PAGE = "https://github.com/Flashbibi/i-dont-wanna-research-stuff/releases/tag/"
TIMEOUT_SECONDS = 3
CACHE_SECONDS_ERFOLG = 24 * 60 * 60
CACHE_SECONDS_FEHLSCHLAG = 60 * 60

TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class Release(NamedTuple):
    tag: str
    version: tuple[int, int, int]
    url: str


# Absichtlich ohne Sperre, weil zwei parallele Abfragen schlimmstenfalls einen zweiten
# GET kosten.
_cache: tuple[float, Release | None] | None = None


def _aktiviert() -> bool:
    return os.environ.get("BESCHAFFUNG_UPDATE_CHECK", "on").strip().lower() != "off"


def _parse_tag(tag: object) -> tuple[int, int, int] | None:
    """``vX.Y.Z`` als Tupel; alles andere ist kein Tag, das wir vergleichen."""
    if not isinstance(tag, str):
        return None
    treffer = TAG_PATTERN.match(tag.strip())
    if treffer is None:
        return None
    major, minor, patch = treffer.groups()
    return int(major), int(minor), int(patch)


def _fetch() -> object:
    """Der einzige Netzzugriff des Moduls - in Tests der Ansatzpunkt."""
    request = urllib.request.Request(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"beschaffung/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def _read_release() -> Release | None:
    try:
        payload = _fetch()
        version = _parse_tag(
            payload.get("tag_name") if isinstance(payload, dict) else None
        )
    except Exception:  # noqa: BLE001 - jeder Fehler heisst schlicht "kein Banner"
        return None
    if version is None:
        return None
    # Tag und Link entstehen aus den geprüften Zahlen, nicht aus der Antwort.
    tag = "v" + ".".join(str(teil) for teil in version)
    return Release(tag, version, RELEASE_PAGE + tag)


def latest_release() -> Release | None:
    """Neuestes Release laut GitHub, wobei auch der Fehlschlag kurz im Cache bleibt,
    damit ein ausgefallenes GitHub nicht jeden Seitenaufbau blockiert."""
    global _cache
    if not _aktiviert():
        return None
    jetzt = time.monotonic()
    if _cache is not None and jetzt < _cache[0]:
        return _cache[1]
    release = _read_release()
    dauer = CACHE_SECONDS_ERFOLG if release is not None else CACHE_SECONDS_FEHLSCHLAG
    _cache = (jetzt + dauer, release)
    return release


def update_available() -> Release | None:
    """Release-Info nur, wenn GitHub strikt neuer ist als der laufende Stand."""
    laufend = _parse_tag(f"v{__version__}")
    if laufend is None:
        return None
    release = latest_release()
    if release is None or release.version <= laufend:
        return None
    return release
