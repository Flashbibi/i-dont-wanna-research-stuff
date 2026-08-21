"""Dünner Integrationstest-Layer der Repository-Schicht gegen echtes Postgres.

Warum es diese Datei gibt: die Fake-Repositories der übrigen Tests liefern
jeden Schlüssel mit, den der Service setzt - auch wenn das echte SQL ihn gar
nicht schreibt oder eine Constraint ihn ablehnt. Diese Klasse hat dreimal
zugeschlagen:

* ``plattform_geprueft_am`` fehlte in ``optimization_input``
* ``shop.land`` war im Schema noch auf ``'CH'`` festgenagelt
* ``lieferziel_id`` wurde im INSERT von ``create_shop`` verworfen

Statt weiterer SQL-String-Prüfungen fahren hier wenige echte Runden: schreiben,
zurücklesen, vergleichen. Die Tests legen dafür eine eigene Wegwerf-Datenbank
an und räumen sie wieder ab; ohne erreichbares Postgres überspringen sie sich.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import PostgresRepository
from app.migrations import apply_migrations, discover_migrations, run_migrations
from app.procurement import ProcurementService

psycopg = pytest.importorskip("psycopg")

DEV_URL = os.environ.get(
    "BESCHAFFUNG_TEST_DATABASE_URL",
    "postgresql://beschaffung:beschaffung@127.0.0.1:5433/beschaffung",
)
TEST_DB = "beschaffung_integration"
MIGRATION_TEST_DB = "beschaffung_migration_016"


def _admin_url() -> str:
    return DEV_URL.rsplit("/", 1)[0] + "/postgres"


def _test_url() -> str:
    return DEV_URL.rsplit("/", 1)[0] + "/" + TEST_DB


def _database_url(name: str) -> str:
    return DEV_URL.rsplit("/", 1)[0] + "/" + name


def test_migration_016_runs_backfills_once_and_is_idempotent(tmp_path):
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    try:
        with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{MIGRATION_TEST_DB}"')
            admin.execute(f'CREATE DATABASE "{MIGRATION_TEST_DB}"')
    except Exception as error:  # noqa: BLE001 - ohne DB wird uebersprungen
        pytest.skip(f"Kein erreichbares Postgres für Integrationstests: {error}")

    try:
        for migration in discover_migrations(migrations_dir):
            if migration.version < 16:
                (tmp_path / migration.path.name).write_text(
                    migration.path.read_text(encoding="utf-8"), encoding="utf-8"
                )
        with psycopg.connect(_database_url(MIGRATION_TEST_DB)) as connection:
            apply_migrations(connection, tmp_path)
            stock_ids = [
                connection.execute(
                    "INSERT INTO stock(bezeichnung, menge) VALUES (%s, %s) RETURNING id",
                    (name, amount),
                ).fetchone()[0]
                for name, amount in (("Altbestand A", 3), ("Altbestand B", 7), ("Leer", 0))
            ]
            connection.commit()

            migration_016 = migrations_dir / "016_stock_bewegung.sql"
            (tmp_path / migration_016.name).write_text(
                migration_016.read_text(encoding="utf-8"), encoding="utf-8"
            )
            assert apply_migrations(connection, tmp_path) == [16]

            assert connection.execute(
                "SELECT to_regclass('public.stock_bewegung')"
            ).fetchone()[0] == "stock_bewegung"
            constraint_defs = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'stock_bewegung'::regclass
                    """
                ).fetchall()
            ]
            assert any("FOREIGN KEY (stock_id) REFERENCES stock(id)" in value for value in constraint_defs)
            assert any("FOREIGN KEY (line_id) REFERENCES bom_line(id) ON DELETE SET NULL" in value for value in constraint_defs)
            assert any("delta <> 0" in value for value in constraint_defs)
            assert any("grund" in value and "uebernahme_migration" in value for value in constraint_defs)
            assert any("kommentar" in value and "korrektur" in value for value in constraint_defs)
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'stock_bewegung'"
                ).fetchall()
            }
            assert {"idx_stock_bewegung_stock", "idx_stock_bewegung_line"} <= indexes

            movements = connection.execute(
                """
                SELECT stock_id, delta, grund, kommentar
                FROM stock_bewegung ORDER BY stock_id
                """
            ).fetchall()
            assert movements == [
                (stock_ids[0], 3, "uebernahme_migration", "Backfill Migration 016"),
                (stock_ids[1], 7, "uebernahme_migration", "Backfill Migration 016"),
            ]

            connection.execute(migration_016.read_text(encoding="utf-8"))
            connection.commit()
            assert connection.execute("SELECT count(*) FROM stock_bewegung").fetchone()[0] == 2
            assert apply_migrations(connection, tmp_path) == []
    finally:
        with psycopg.connect(_admin_url(), connect_timeout=5, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{MIGRATION_TEST_DB}"')


@pytest.fixture(scope="module")
def repository():
    try:
        with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
            admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    except Exception as error:  # noqa: BLE001 - ohne DB wird uebersprungen
        pytest.skip(f"Kein erreichbares Postgres für Integrationstests: {error}")

    run_migrations(_test_url())
    yield PostgresRepository(_test_url())

    with psycopg.connect(_admin_url(), connect_timeout=5, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


@pytest.fixture
def service(repository):
    return ProcurementService(repository)


def test_the_schema_applies_from_scratch_and_derives_the_home_address(repository):
    ziele = repository.list_lieferziele()

    # Migration 012 leitet die Heimadresse ab, auch auf leerer Datenbank.
    assert [ziel["name"] for ziel in ziele] == ["Zuhause (CH)"]
    assert ziele[0]["land"] == "CH"
    assert ziele[0]["waehrung"] == "CHF"


def test_a_shop_keeps_its_delivery_target_through_the_insert(service, repository):
    """Der Fall, den das Fake nicht sehen konnte: INSERT verwirft eine Spalte."""
    ziel = service.record_lieferziel("Postfach (DE)", "Grenzweg 1", "DE", aufschlag_chf=25, zuschlag_tage=3)

    shop = service.record_shop(
        "Reichelt", "https://www.reichelt.de/", "DE", 5.95, 100, None, 3,
        "https://www.reichelt.de/versand", "Versand 5,95 EUR",
    )

    assert shop["lieferziel_id"] == ziel["id"]
    assert shop["land"] == "DE"
    # Und zurückgelesen ist es immer noch da.
    assert repository.get_shop(shop["id"])["lieferziel_id"] == ziel["id"]


def test_a_non_swiss_shop_is_allowed_by_the_schema(service):
    """Migration 013: shop.land war auf 'CH' festgenagelt."""
    service.record_lieferziel("Wien", "Ringstrasse 1", "AT")

    shop = service.record_shop(
        "Conrad AT", "https://www.conrad.at/", "AT", 6.0, None, None, 4,
        "https://www.conrad.at/versand", "Versand 6 EUR",
    )

    assert shop["land"] == "AT"


def test_currency_evidence_survives_the_round_trip(service, repository):
    ziel = service.record_lieferziel("Postfach DE2", "Grenzweg 2", "DE")
    # Zweites DE-Ziel: die Zuordnung muss jetzt explizit sein.
    shop = service.record_shop(
        "Pollin", "https://www.pollin.de/", "DE", 4.9, None, None, 3,
        "https://www.pollin.de/versand", "Versand 4,90 EUR", lieferziel_id=ziel["id"],
    )
    repository.save_kurs("EUR", Decimal("0.94"), ProcurementService._heute(),
                         "https://api.frankfurter.app/latest?from=EUR&to=CHF")
    job = service.create_job("1x Kondensator")
    line_id = repository.get_job(job["job_id"])["lines"][0]["id"]

    angebot = service.record_offer(
        line_id, shop["id"], "Kondensator", "https://www.pollin.de/kondensator",
        "7.99", lieferzeit_text="2 Tage", artikelnummer="ART-4711", waehrung="EUR",
    )

    assert angebot["preis_original"] == Decimal("7.99")
    assert angebot["waehrung"] == "EUR"
    assert angebot["preis_chf"] == Decimal("7.51")
    assert angebot["artikelnummer"] == "ART-4711"

    # Und der Weg, den der Optimierer nimmt, führt dieselben Spalten.
    daten = repository.optimization_input(job["job_id"])
    zeile = daten["offers"][0]
    for spalte in ("preis_original", "waehrung", "kurs", "kurs_am", "kurs_quelle", "artikelnummer"):
        assert zeile[spalte] is not None, f"{spalte} fehlt in optimization_input"
    shop_zeile = daten["shops"][0]
    for spalte in ("lieferziel_id", "lieferziel_name", "lieferziel_land",
                   "lieferziel_aufschlag_chf", "lieferziel_zuschlag_tage"):
        assert spalte in shop_zeile, f"{spalte} fehlt in optimization_input"


def test_the_database_refuses_a_converted_price_without_evidence(service, repository):
    """Die Hauswand aus Migration 011, an der echten Datenbank."""
    job = service.create_job("1x Widerstand")
    line_id = repository.get_job(job["job_id"])["lines"][0]["id"]
    # Eigener CH-Shop, damit die Waehrung zum Ziel passt.
    shop = service.record_shop(
        "Distrelec", "https://www.distrelec.ch/", "CH", 8.0, None, None, 2,
        "https://www.distrelec.ch/versand", "Versand CHF 8",
    )
    angebot = service.record_offer(
        line_id, shop["id"], "Widerstand", "https://www.distrelec.ch/widerstand", "1.50",
        lieferzeit_text="2 Tage",
    )

    with psycopg.connect(_test_url()) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE offer SET waehrung = 'EUR', kurs_quelle = NULL WHERE id = %s",
                (angebot["id"],),
            )


def test_the_platform_finding_needs_evidence_at_the_database(repository):
    shop = repository.list_shops()[0]

    with pytest.raises(ValueError):
        repository.save_shop_platform(shop["id"], "opencart", "   ")

    gespeichert = repository.save_shop_platform(shop["id"], "opencart", "Cookie OCSESSID")
    assert gespeichert["plattform"] == "opencart"
    assert repository.get_shop(shop["id"])["plattform_geprueft_am"] is not None
