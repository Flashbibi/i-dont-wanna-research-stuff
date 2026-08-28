"""Gebündelte Adapter müssen fehlerfrei laden - sonst bricht der Build."""

from pathlib import Path

import pytest

from app.adapter import lade_adapter

GEBUENDELT = sorted(Path("adapters").glob("*.yaml"))


@pytest.mark.parametrize("pfad", GEBUENDELT, ids=lambda pfad: pfad.name)
def test_gebuendelter_adapter_laedt(pfad: Path) -> None:
    adapter = lade_adapter(pfad)
    assert adapter.id
    assert adapter.domain
