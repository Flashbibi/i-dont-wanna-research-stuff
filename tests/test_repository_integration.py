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
from app.procurement import ProcurementService, ValidationError

psycopg = pytest.importorskip("psycopg")

DEV_URL = os.environ.get(
    "BESCHAFFUNG_TEST_DATABASE_URL",
    "postgresql://beschaffung:beschaffung@127.0.0.1:5433/beschaffung",
)
TEST_DB = "beschaffung_integration"
MIGRATION_TEST_DB = "beschaffung_migration_016"
E2E_TEST_DB = "beschaffung_e2e_shops"


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
                for name, amount in (
                    ("Altbestand A", 3),
                    ("Altbestand B", 7),
                    ("Leer", 0),
                )
            ]
            connection.commit()

            migration_016 = migrations_dir / "016_stock_bewegung.sql"
            (tmp_path / migration_016.name).write_text(
                migration_016.read_text(encoding="utf-8"), encoding="utf-8"
            )
            assert apply_migrations(connection, tmp_path) == [16]

            assert (
                connection.execute(
                    "SELECT to_regclass('public.stock_bewegung')"
                ).fetchone()[0]
                == "stock_bewegung"
            )
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
            assert any(
                "FOREIGN KEY (stock_id) REFERENCES stock(id)" in value
                for value in constraint_defs
            )
            assert any(
                "FOREIGN KEY (line_id) REFERENCES bom_line(id) ON DELETE SET NULL"
                in value
                for value in constraint_defs
            )
            assert any("delta <> 0" in value for value in constraint_defs)
            assert any(
                "grund" in value and "uebernahme_migration" in value
                for value in constraint_defs
            )
            assert any(
                "kommentar" in value and "korrektur" in value
                for value in constraint_defs
            )
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
            assert (
                connection.execute("SELECT count(*) FROM stock_bewegung").fetchone()[0]
                == 2
            )
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
    ziel = service.record_lieferziel(
        "Postfach (DE)", "Grenzweg 1", "DE", aufschlag_chf=25, zuschlag_tage=3
    )

    shop = service.record_shop(
        "Reichelt",
        "https://www.reichelt.de/",
        "DE",
        5.95,
        100,
        None,
        3,
        "https://www.reichelt.de/versand",
        "Versand 5,95 EUR",
    )

    assert shop["lieferziel_id"] == ziel["id"]
    assert shop["land"] == "DE"
    # Und zurückgelesen ist es immer noch da.
    assert repository.get_shop(shop["id"])["lieferziel_id"] == ziel["id"]


def test_a_non_swiss_shop_is_allowed_by_the_schema(service):
    """Migration 013: shop.land war auf 'CH' festgenagelt."""
    service.record_lieferziel("Wien", "Ringstrasse 1", "AT")

    shop = service.record_shop(
        "Conrad AT",
        "https://www.conrad.at/",
        "AT",
        6.0,
        None,
        None,
        4,
        "https://www.conrad.at/versand",
        "Versand 6 EUR",
    )

    assert shop["land"] == "AT"


def test_currency_evidence_survives_the_round_trip(service, repository):
    ziel = service.record_lieferziel("Postfach DE2", "Grenzweg 2", "DE")
    # Zweites DE-Ziel: die Zuordnung muss jetzt explizit sein.
    shop = service.record_shop(
        "Pollin",
        "https://www.pollin.de/",
        "DE",
        4.9,
        None,
        None,
        3,
        "https://www.pollin.de/versand",
        "Versand 4,90 EUR",
        lieferziel_id=ziel["id"],
    )
    repository.save_kurs(
        "EUR",
        Decimal("0.94"),
        ProcurementService._heute(),
        "https://api.frankfurter.app/latest?from=EUR&to=CHF",
    )
    job = service.create_job("1x Kondensator")
    line_id = repository.get_job(job["job_id"])["lines"][0]["id"]

    angebot = service.record_offer(
        line_id,
        shop["id"],
        "Kondensator",
        "https://www.pollin.de/kondensator",
        "7.99",
        lieferzeit_text="2 Tage",
        artikelnummer="ART-4711",
        waehrung="EUR",
    )

    assert angebot["preis_original"] == Decimal("7.99")
    assert angebot["waehrung"] == "EUR"
    assert angebot["preis_chf"] == Decimal("7.51")
    assert angebot["artikelnummer"] == "ART-4711"

    # Und der Weg, den der Optimierer nimmt, führt dieselben Spalten.
    daten = repository.optimization_input(job["job_id"])
    zeile = daten["offers"][0]
    for spalte in (
        "preis_original",
        "waehrung",
        "kurs",
        "kurs_am",
        "kurs_quelle",
        "artikelnummer",
    ):
        assert zeile[spalte] is not None, f"{spalte} fehlt in optimization_input"
    shop_zeile = daten["shops"][0]
    for spalte in (
        "lieferziel_id",
        "lieferziel_name",
        "lieferziel_land",
        "lieferziel_aufschlag_chf",
        "lieferziel_zuschlag_tage",
    ):
        assert spalte in shop_zeile, f"{spalte} fehlt in optimization_input"


def test_the_database_refuses_a_converted_price_without_evidence(service, repository):
    """Die Hauswand aus Migration 011, an der echten Datenbank."""
    job = service.create_job("1x Widerstand")
    line_id = repository.get_job(job["job_id"])["lines"][0]["id"]
    # Eigener CH-Shop, damit die Waehrung zum Ziel passt.
    shop = service.record_shop(
        "Distrelec",
        "https://www.distrelec.ch/",
        "CH",
        8.0,
        None,
        None,
        2,
        "https://www.distrelec.ch/versand",
        "Versand CHF 8",
    )
    angebot = service.record_offer(
        line_id,
        shop["id"],
        "Widerstand",
        "https://www.distrelec.ch/widerstand",
        "1.50",
        lieferzeit_text="2 Tage",
    )

    with psycopg.connect(_test_url()) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE offer SET waehrung = 'EUR', kurs_quelle = NULL WHERE id = %s",
                (angebot["id"],),
            )


def test_the_capture_path_survives_the_round_trip(service, repository):
    """Migration 017: wer das Angebot erfasst hat, steht am Angebot."""
    job_id, line_id = _create_line(service, repository, "1x Servohalter")
    shop = _create_shop(service, "erfassung")

    von_hand = _create_offer(service, line_id, shop, "servohalter", "ART-1")
    per_adapter = service.record_offer(
        line_id,
        shop["id"],
        "Servohalter",
        f"{shop['url'].rstrip('/')}/servohalter-v2",
        "2.00",
        lieferzeit_text="2 Tage",
        erfasst_via="adapter:demo",
    )

    # NULL heisst weiterhin "von Hand bzw. via KI erfasst".
    assert von_hand["erfasst_via"] is None
    assert per_adapter["erfasst_via"] == "adapter:demo"

    with psycopg.connect(_test_url()) as connection:
        gespeichert = connection.execute(
            "SELECT erfasst_via FROM offer WHERE id = %s", (per_adapter["id"],)
        ).fetchone()[0]
        assert gespeichert == "adapter:demo"
        # Und ein leerer Erfassungsweg ist keiner.
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE offer SET erfasst_via = '   ' WHERE id = %s",
                (per_adapter["id"],),
            )


def test_the_newest_observation_of_a_pair_is_what_a_refresh_starts_from(
    service, repository
):
    """Tagesgenaue Historie: get_offer liefert die jüngste Zeile ihrer Reihe."""
    job_id, line_id = _create_line(service, repository, "1x Servohalter alt")
    shop = _create_shop(service, "auffrischen")
    url = f"{shop['url'].rstrip('/')}/servohalter"

    gestern = service.record_offer(
        line_id,
        shop["id"],
        "Servohalter",
        url,
        "12.90",
        lieferzeit_text="3 Tage",
        erfasst_via="adapter:demo",
    )
    with psycopg.connect(_test_url()) as connection:
        # Die Beobachtung von gestern - danach legt derselbe Aufruf eine neue an.
        connection.execute(
            "UPDATE offer SET beobachtungstag = CURRENT_DATE - 1 WHERE id = %s",
            (gestern["id"],),
        )
    heute = service.record_offer(
        line_id,
        shop["id"],
        "Servohalter",
        url,
        "11.50",
        lieferzeit_text="2 Tage",
        erfasst_via="adapter:demo",
    )

    assert heute["id"] != gestern["id"]
    # Wer die alte ID in der Hand hält, bekommt trotzdem den heutigen Stand.
    juengste = repository.get_offer(gestern["id"])
    assert juengste["id"] == heute["id"]
    assert juengste["preis_chf"] == Decimal("11.50")
    assert juengste["lieferzeit_tage"] == 2
    assert repository.get_offer(999_999) is None

    # Und der Erfassungsweg reist bis in die Angebote der Job-UI mit.
    daten = repository.optimization_input(job_id)
    zeile = next(row for row in daten["offers"] if row["id"] == heute["id"])
    assert zeile["erfasst_via"] == "adapter:demo"


def test_the_platform_finding_needs_evidence_at_the_database(repository):
    shop = repository.list_shops()[0]

    with pytest.raises(ValueError):
        repository.save_shop_platform(shop["id"], "opencart", "   ")

    gespeichert = repository.save_shop_platform(
        shop["id"], "opencart", "Cookie OCSESSID"
    )
    assert gespeichert["plattform"] == "opencart"
    assert repository.get_shop(shop["id"])["plattform_geprueft_am"] is not None


def _assert_stock_invariant(connection):
    rows = connection.execute(
        """
        SELECT s.id, s.menge, SUM(b.delta)::int AS bewegungssumme
        FROM stock s JOIN stock_bewegung b ON b.stock_id = s.id
        GROUP BY s.id, s.menge ORDER BY s.id
        """
    ).fetchall()
    assert rows
    assert all(row[1] == row[2] for row in rows)


def _create_line(service, repository, text):
    job_id = service.create_job(text)["job_id"]
    line_id = repository.get_job(job_id)["lines"][0]["id"]
    return job_id, line_id


def _create_shop(service, slug):
    return service.record_shop(
        f"Integration {slug}",
        f"https://{slug}.example.ch/",
        "CH",
        5,
        None,
        None,
        2,
        f"https://{slug}.example.ch/versand",
        "Versand CHF 5",
    )


def _create_offer(service, line_id, shop, slug, article):
    return service.record_offer(
        line_id,
        shop["id"],
        slug,
        f"{shop['url'].rstrip('/')}/{slug}",
        "1.00",
        lieferzeit_text="2 Tage",
        artikelnummer=article,
    )


def _create_stock_from_offer(repository, job_id, line_id, offer_id, name, amount):
    with psycopg.connect(_test_url()) as connection:
        purchase_id = connection.execute(
            """
            INSERT INTO purchase(job_id, variante, total_chf, bestellt_am, zugesagt_liefertage)
            VALUES (%s, '{}'::jsonb, 1, NOW(), '{}'::jsonb) RETURNING id
            """,
            (job_id,),
        ).fetchone()[0]
        item_id = connection.execute(
            """
            INSERT INTO purchase_item(purchase_id, line_id, offer_id, menge, einzelpreis_chf)
            VALUES (%s, %s, %s, %s, 1) RETURNING id
            """,
            (purchase_id, line_id, offer_id, amount),
        ).fetchone()[0]
        stock_id = connection.execute(
            """
            INSERT INTO stock(bezeichnung, menge, purchase_item_id)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (name, amount, item_id),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO stock_bewegung(stock_id, delta, grund, kommentar)
            VALUES (%s, %s, 'uebernahme_migration', 'Integrationstest')
            """,
            (stock_id, amount),
        )
        return stock_id


def _create_plain_stock(name, amount, updated_at="2026-01-01T00:00:00Z"):
    with psycopg.connect(_test_url()) as connection:
        stock_id = connection.execute(
            """
            INSERT INTO stock(bezeichnung, menge, aktualisiert_am)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (name, amount, updated_at),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO stock_bewegung(stock_id, delta, grund, kommentar)
            VALUES (%s, %s, 'uebernahme_migration', 'Integrationstest')
            """,
            (stock_id, amount),
        )
        return stock_id


def test_article_number_match_works_with_different_text(service, repository):
    shop = _create_shop(service, "article-match")
    source_job, source_line = _create_line(service, repository, "4x Ursprungsprodukt")
    source_offer = _create_offer(
        service, source_line, shop, "source-article", "ART-MATCH-1"
    )
    stock_id = _create_stock_from_offer(
        repository,
        source_job,
        source_line,
        source_offer["id"],
        "Völlig andere Bezeichnung",
        4,
    )
    _, target_line = _create_line(
        service, repository, "3x Zielprodukt ohne Texttreffer"
    )
    _create_offer(service, target_line, shop, "target-article", "ART-MATCH-1")

    service.mark_line(target_line, "bestand")

    with psycopg.connect(_test_url()) as connection:
        assert (
            connection.execute(
                "SELECT menge FROM stock WHERE id = %s", (stock_id,)
            ).fetchone()[0]
            == 1
        )


def test_article_number_match_refuses_a_different_shop(service, repository):
    source_shop = _create_shop(service, "article-shop-a")
    target_shop = _create_shop(service, "article-shop-b")
    source_job, source_line = _create_line(service, repository, "2x Fremder Ursprung")
    source_offer = _create_offer(
        service, source_line, source_shop, "source-shop-a", "ART-SHOP-1"
    )
    stock_id = _create_stock_from_offer(
        repository, source_job, source_line, source_offer["id"], "Kein Texttreffer", 2
    )
    _, target_line = _create_line(service, repository, "2x Anderes Ziel")
    _create_offer(service, target_line, target_shop, "target-shop-b", "ART-SHOP-1")

    with pytest.raises(ValidationError, match="deckt"):
        service.mark_line(target_line, "bestand")

    with psycopg.connect(_test_url()) as connection:
        assert (
            connection.execute(
                "SELECT menge FROM stock WHERE id = %s", (stock_id,)
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM stock_bewegung WHERE line_id = %s", (target_line,)
            ).fetchone()[0]
            == 0
        )


def test_explicit_stock_ids_refuse_insufficient_coverage(service, repository):
    _, line_id = _create_line(service, repository, "3x Expliziter Auswahltest")
    stock_id = _create_plain_stock("Bewusst andere Bezeichnung", 2)

    with pytest.raises(ValidationError, match="deckt"):
        service.mark_line(line_id, "bestand", stock_ids=[stock_id])

    with psycopg.connect(_test_url()) as connection:
        assert (
            connection.execute(
                "SELECT menge FROM stock WHERE id = %s", (stock_id,)
            ).fetchone()[0]
            == 2
        )


def test_stock_candidates_rank_by_common_tokens_then_recency(service, repository):
    _, line_id = _create_line(service, repository, "1x rangservo rangmotor ranghalter")
    entries = [
        ("rangservo rangmotor alt", "2026-01-01T00:00:00Z"),
        ("rangservo rangmotor neu", "2026-01-02T00:00:00Z"),
        ("rangservo kandidat-a", "2026-01-03T00:00:00Z"),
        ("rangmotor kandidat-b", "2026-01-04T00:00:00Z"),
        ("ranghalter kandidat-c", "2026-01-05T00:00:00Z"),
        ("rangservo kandidat-d", "2026-01-06T00:00:00Z"),
    ]
    for name, updated_at in entries:
        _create_plain_stock(name, 1, updated_at)

    result = service.check_stock(line_id)

    assert [row["bezeichnung"] for row in result["kandidaten"]] == [
        "rangservo rangmotor neu",
        "rangservo rangmotor alt",
        "rangservo kandidat-d",
        "ranghalter kandidat-c",
        "rangmotor kandidat-b",
    ]


def test_deleting_a_stock_fulfilled_job_refunds_stock_and_preserves_history(
    service, repository
):
    job = service.create_job("3x Servo")
    line_id = repository.get_job(job["job_id"])["lines"][0]["id"]
    with psycopg.connect(_test_url()) as connection:
        stock_id = connection.execute(
            "INSERT INTO stock(bezeichnung, menge) VALUES ('Servo', 5) RETURNING id"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO stock_bewegung(stock_id, delta, grund, kommentar)
            VALUES (%s, 5, 'uebernahme_migration', 'Testbestand')
            """,
            (stock_id,),
        )

    service.mark_line(line_id, "bestand")
    service.delete_job(job["job_id"], job["job_id"])

    with psycopg.connect(_test_url()) as connection:
        assert (
            connection.execute(
                "SELECT menge FROM stock WHERE id = %s", (stock_id,)
            ).fetchone()[0]
            == 5
        )
        movements = connection.execute(
            """
            SELECT delta, grund, line_id, kommentar
            FROM stock_bewegung WHERE stock_id = %s ORDER BY id
            """,
            (stock_id,),
        ).fetchall()
        assert movements == [
            (5, "uebernahme_migration", None, "Testbestand"),
            (-3, "abgang_bestand", None, None),
            (
                3,
                "rueckbuchung_job_geloescht",
                None,
                f"Rückbuchung: Job {job['job_id']} gelöscht",
            ),
        ]
        _assert_stock_invariant(connection)


@pytest.fixture
def frische_datenbank():
    """Eine eigene, frisch migrierte Datenbank pro Test.

    Der Füllstand der Shoptabelle ist in den folgenden Tests der Prüfgegenstand.
    Die Modul-Datenbank oben taugt dafür nicht: sie ist zu diesem Zeitpunkt
    voller Shops aus den Tests davor, und «leer» wäre dort eine Frage der
    Reihenfolge statt eine Zusicherung.
    """
    try:
        with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{E2E_TEST_DB}"')
            admin.execute(f'CREATE DATABASE "{E2E_TEST_DB}"')
    except Exception as error:  # noqa: BLE001 - ohne DB wird uebersprungen
        pytest.skip(f"Kein erreichbares Postgres für Integrationstests: {error}")

    run_migrations(_database_url(E2E_TEST_DB))
    yield PostgresRepository(_database_url(E2E_TEST_DB))

    with psycopg.connect(_admin_url(), connect_timeout=5, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{E2E_TEST_DB}"')


def test_a_test_job_supplies_its_own_shops_when_the_table_is_empty(frische_datenbank):
    """Der Klickpfad endete auf frischer Datenbank in HTTP 422 - hier nicht mehr."""
    repository = frische_datenbank
    assert repository.list_shops() == []

    testjob = repository.create_e2e_test_job()

    shops = repository.list_shops()
    assert [shop["name"] for shop in shops] == [
        "[E2E-TEST] Shop A",
        "[E2E-TEST] Shop B",
        "[E2E-TEST] Shop C",
    ]
    assert [shop["domain"] for shop in shops] == [
        "e2e-a.invalid",
        "e2e-b.invalid",
        "e2e-c.invalid",
    ]
    assert testjob["created_shop_ids"] == [shop["id"] for shop in shops]
    for shop in shops:
        # Provenienz gilt auch für Wegwerfdaten: wo ein Versandwert steht,
        # stehen der wörtliche Text und seine Quelle daneben.
        assert shop["status"] == "bestaetigt"
        assert shop["versand_text"].startswith("[E2E-TEST]")
        assert shop["profil_quelle_url"].startswith("https://e2e.invalid/")
        assert shop["versand_chf"] == shop["versand_original"] == Decimal("5.00")
        assert shop["versand_waehrung"] == "CHF"
        assert shop["lieferziel_land"] == "CH"

    # Und der Testgraph steht: der Optimierer rechnet mit diesen Shops, und die
    # Pläne nennen die Shop-URL, an der der Klickpfad weitermacht.
    plaene = ProcurementService(repository).plan_scenarios(testjob["job_id"], tempo=0.5)
    urls = [shop["url"] for plan in plaene["scenarios"] for shop in plan["shops"]]
    assert plaene["scenarios"] and urls
    assert all(url.endswith(".invalid/") for url in urls)


def test_deleting_a_test_job_takes_its_disposable_shops_with_it(frische_datenbank):
    repository = frische_datenbank
    testjob = repository.create_e2e_test_job()

    ergebnis = repository.delete_e2e_test_job(testjob["job_id"])

    assert ergebnis["deleted"] is True
    assert sorted(ergebnis["deleted_shop_ids"]) == sorted(testjob["created_shop_ids"])
    assert repository.list_shops() == []
    assert repository.get_job(testjob["job_id"]) is None


def test_a_second_test_job_keeps_the_shared_disposable_shops_alive(frische_datenbank):
    """Zwei Testjobs teilen sich die Wegwerf-Shops; der erste Cleanup lässt sie stehen."""
    repository = frische_datenbank
    erster = repository.create_e2e_test_job()
    zweiter = repository.create_e2e_test_job()

    assert zweiter["created_shop_ids"] == []
    assert zweiter["shop_ids"] == erster["shop_ids"]

    assert repository.delete_e2e_test_job(erster["job_id"])["deleted_shop_ids"] == []
    assert [shop["id"] for shop in repository.list_shops()] == erster["shop_ids"]

    aufgeraeumt = repository.delete_e2e_test_job(zweiter["job_id"])
    assert sorted(aufgeraeumt["deleted_shop_ids"]) == sorted(erster["created_shop_ids"])
    assert repository.list_shops() == []


def test_only_the_missing_shops_are_created_and_only_those_are_removed(
    frische_datenbank,
):
    """Ein echter Shop steht schon da: dann kommen zwei dazu, nicht drei."""
    repository = frische_datenbank
    echter = _create_shop(ProcurementService(repository), "echt")

    testjob = repository.create_e2e_test_job()

    assert len(testjob["created_shop_ids"]) == 2
    assert testjob["shop_ids"][0] == echter["id"]
    assert [
        shop["domain"]
        for shop in repository.list_shops()
        if shop["domain"].endswith(".invalid")
    ] == ["e2e-a.invalid", "e2e-b.invalid"]

    ergebnis = repository.delete_e2e_test_job(testjob["job_id"])

    assert sorted(ergebnis["deleted_shop_ids"]) == sorted(testjob["created_shop_ids"])
    assert [shop["id"] for shop in repository.list_shops()] == [echter["id"]]


def test_a_populated_shop_table_is_left_exactly_as_it_was(frische_datenbank):
    """Drei echte Shops sind da: dann legt der Testjob keinen an und löscht keinen."""
    repository = frische_datenbank
    service = ProcurementService(repository)
    for slug in ("alpha", "beta", "gamma"):
        _create_shop(service, slug)
    vorher = repository.list_shops()

    testjob = repository.create_e2e_test_job()

    assert testjob["created_shop_ids"] == []
    assert testjob["shop_ids"] == sorted(shop["id"] for shop in vorher)

    ergebnis = repository.delete_e2e_test_job(testjob["job_id"])

    assert ergebnis["deleted_shop_ids"] == []
    assert repository.list_shops() == vorher
