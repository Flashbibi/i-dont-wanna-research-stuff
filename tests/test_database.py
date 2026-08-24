# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
from datetime import date, datetime, timedelta

import pytest

from app.database import PostgresRepository, decode_database_value


class Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def assert_stock_invariant(stock_rows, movements):
    totals = {}
    for movement in movements:
        totals[movement["stock_id"]] = totals.get(movement["stock_id"], 0) + movement["delta"]
    for stock in stock_rows:
        if stock["id"] in totals:
            assert stock["menge"] == totals[stock["id"]]


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM bom_line WHERE id" in normalized:
            return Result(one={"id": 1, "job_id": 1, "suchtext": "Servo", "menge": 1})
        if "lower(%s)" in normalized:
            raise RuntimeError("PostgreSQL would infer the placeholder as bytea")
        return Result(many=[])


def test_check_line_explicitly_casts_case_insensitive_query_parameters_to_text():
    repository = PostgresRepository("unused")
    repository._connect = lambda: Connection()

    checked = repository.check_line(1)

    assert checked["stock"] == []
    assert checked["previous_purchases"] == []
    assert checked["cached_offers"] == []


def test_database_value_decoder_normalizes_sql_ascii_text_bytes():
    assert decode_database_value(b"bastelgarage.ch") == "bastelgarage.ch"
    assert decode_database_value("ungeprueft") == "ungeprueft"


class JobListConnection(Connection):
    def __init__(self):
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        return Result(many=[])


def test_job_list_excludes_marked_e2e_jobs():
    repository = PostgresRepository("unused")
    connection = JobListConnection()
    repository._connect = lambda: connection

    repository.list_jobs()

    assert "WHERE NOT j.is_test" in connection.sql


def test_shop_list_includes_delivery_target_and_shipping_provenance():
    repository = PostgresRepository("unused")
    connection = JobListConnection()
    repository._connect = lambda: connection

    repository.list_shops()

    for field in (
        "s.lieferziel_id",
        "lieferziel_name",
        "lieferziel_land",
        "lieferziel_waehrung",
        "versand_original",
        "versand_waehrung",
        "versand_kurs",
        "versand_kurs_am",
        "versand_kurs_quelle",
    ):
        assert field in connection.sql
    assert "LEFT JOIN lieferziel" in connection.sql
    assert "ORDER BY lower(s.name), s.id" in connection.sql


class JobDeletionConnection(Connection):
    def __init__(self, job):
        self.job = job
        self.statements = []

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if "FROM job j" in normalized and "FOR UPDATE" in normalized:
            return Result(one={
                "id": self.job["id"],
                "status": self.job["status"],
                "is_test": self.job["is_test"],
            })
        if "FROM bom_line" in normalized and "FOR UPDATE" in normalized:
            status = "in_arbeit" if self.job["has_progress"] else "offen"
            return Result(many=[{"id": 46, "status": status}])
        if "AS has_offers" in normalized:
            return Result(one={
                "has_offers": self.job["has_offers"],
                "has_purchase": self.job["has_purchase"],
            })
        return Result()


def test_delete_unstarted_job_deletes_only_lines_and_exact_guarded_job():
    repository = PostgresRepository("unused")
    connection = JobDeletionConnection({
        "id": 13,
        "status": "offen",
        "is_test": False,
        "has_offers": False,
        "has_purchase": False,
        "has_progress": False,
    })
    repository._connect = lambda: connection

    result = repository.delete_unstarted_job(13)

    assert result == {"job_id": 13, "deleted": True}
    job_lock = next(i for i, (sql, _) in enumerate(connection.statements) if "FROM job j" in sql)
    line_lock = next(i for i, (sql, _) in enumerate(connection.statements) if "FROM bom_line" in sql and "FOR UPDATE" in sql)
    state_check = next(i for i, (sql, _) in enumerate(connection.statements) if "AS has_offers" in sql)
    assert job_lock < line_lock < state_check
    assert connection.statements[-2:] == [
        ("DELETE FROM bom_line WHERE job_id = %s", (13,)),
        ("DELETE FROM job WHERE id = %s AND status = 'offen' AND NOT is_test", (13,)),
    ]


@pytest.mark.parametrize(
    "changed, message",
    [
        ({"status": "in_arbeit"}, "nicht mehr unberührt"),
        ({"has_offers": True}, "nicht mehr unberührt"),
        ({"has_purchase": True}, "nicht mehr unberührt"),
        ({"has_progress": True}, "nicht mehr unberührt"),
        ({"is_test": True}, "Test-Job"),
    ],
)
def test_delete_unstarted_job_rejects_any_touched_or_test_job(changed, message):
    job = {
        "id": 13,
        "status": "offen",
        "is_test": False,
        "has_offers": False,
        "has_purchase": False,
        "has_progress": False,
        **changed,
    }
    repository = PostgresRepository("unused")
    connection = JobDeletionConnection(job)
    repository._connect = lambda: connection

    with pytest.raises(ValueError, match=message):
        repository.delete_unstarted_job(13)

    assert not any(sql.startswith("DELETE") for sql, _ in connection.statements)


class StockJobDeletionConnection(JobDeletionConnection):
    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM bom_line" in normalized and "FOR UPDATE" in normalized:
            self.statements.append((normalized, params))
            return Result(many=[{"id": 46, "status": "bestand"}])
        if "FROM stock_bewegung" in normalized and "GROUP BY b.stock_id" in normalized:
            self.statements.append((normalized, params))
            return Result(many=[{"stock_id": 4, "delta_sum": -3}])
        if normalized.startswith("SELECT id FROM stock"):
            self.statements.append((normalized, params))
            return Result(one={"id": 4})
        return super().execute(sql, params)


def test_delete_open_job_refunds_stock_movements_before_deleting_lines():
    repository = PostgresRepository("unused")
    connection = StockJobDeletionConnection({
        "id": 13, "status": "offen", "is_test": False,
        "has_offers": False, "has_purchase": False, "has_progress": False,
    })
    repository._connect = lambda: connection

    repository.delete_unstarted_job(13)

    refund = next(
        (sql, params) for sql, params in connection.statements
        if sql.startswith("INSERT INTO stock_bewegung")
    )
    assert refund[1] == (4, 3, "Rückbuchung: Job 13 gelöscht")
    assert_stock_invariant(
        [{"id": 4, "menge": 5}],
        [
            {"stock_id": 4, "delta": 5},
            {"stock_id": 4, "delta": -3},
            {"stock_id": 4, "delta": 3},
        ],
    )
    refund_index = connection.statements.index(refund)
    delete_index = next(
        index for index, (sql, _) in enumerate(connection.statements)
        if sql == "DELETE FROM bom_line WHERE job_id = %s"
    )
    assert refund_index < delete_index


def test_delete_job_with_stock_line_is_still_rejected_when_job_is_not_open():
    repository = PostgresRepository("unused")
    connection = StockJobDeletionConnection({
        "id": 13, "status": "in_arbeit", "is_test": False,
        "has_offers": False, "has_purchase": False, "has_progress": False,
    })
    repository._connect = lambda: connection

    with pytest.raises(ValueError, match="nicht mehr unberührt"):
        repository.delete_unstarted_job(13)


def test_job_selection_is_saved_as_jsonb():
    class SelectionConnection(Connection):
        def __init__(self):
            self.sql = ""
            self.params = None

        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            self.params = params
            return Result(one={"id": 7, "selected_assignments": {"10": 31}})

    repository = PostgresRepository("unused")
    connection = SelectionConnection()
    repository._connect = lambda: connection

    saved = repository.save_job_selection(7, {"10": 31})

    assert "selected_assignments = %s" in connection.sql
    assert saved["selected_assignments"] == {"10": 31}


def test_optimization_input_selects_the_columns_the_cart_handover_needs():
    """Ohne plattform_geprueft_am kann der Füllknopf nie verschwinden.

    Ein Fake-Repository liefert den Schlüssel immer mit, deshalb prüft dieser
    Test die tatsächliche Spaltenliste statt des Verhaltens.
    """
    statements = []

    class ShopColumnConnection(Connection):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            statements.append(normalized)
            if "FROM job WHERE id" in normalized:
                return Result(one={"status": "in_arbeit", "selected_assignments": None})
            if "FROM offer o" in normalized:
                return Result(many=[{"id": 1, "line_id": 1, "shop_id": 5}])
            return Result(many=[])

    repository = PostgresRepository("unused")
    repository._connect = lambda: ShopColumnConnection()

    repository.optimization_input(1)

    shop_query = next(sql for sql in statements if "FROM shop s LEFT JOIN lieferziel" in sql)
    for column in (
        "plattform",
        "plattform_beleg",
        "plattform_geprueft_am",
        "lieferziel_name",
        "lieferziel_land",
        "lieferziel_aufschlag_chf",
        "lieferziel_zuschlag_tage",
        "versand_original",
        "gratis_ab_original",
        "mindestbestellwert_original",
        "versand_waehrung",
        "versand_kurs",
        "versand_kurs_am",
        "versand_kurs_quelle",
    ):
        assert column in shop_query
    offer_query = next(sql for sql in statements if "FROM offer o" in sql)
    for column in ("o.shop_produkt_id", "o.artikelnummer", "o.preis_original", "o.waehrung", "o.kurs"):
        assert column in offer_query


class HistoryConnection(Connection):
    def __init__(self):
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params
        return Result(
            many=[
                {
                    "purchase_id": 8,
                    "produktname": "Servo Pro",
                    "shop_id": 5,
                    "shop_name": "Swiss Shop",
                    "menge": 2,
                    "einzelpreis_chf": "12.00",
                    "bestellt_am": "2026-08-01T10:00:00Z",
                    "zugesagt_liefertage_pro_shop": {"5": 2},
                    "angekommen_am": "2026-08-03T10:00:00Z",
                }
            ]
        )


def test_search_history_matches_product_or_shop_and_resolves_promised_days():
    repository = PostgresRepository("unused")
    connection = HistoryConnection()
    repository._connect = lambda: connection

    rows = repository.search_history("servo")

    assert "o.produktname ILIKE '%%' ||" in connection.sql
    assert "s.name ILIKE '%%' ||" in connection.sql
    assert connection.params == ("servo", "servo")
    assert rows[0]["zugesagt_liefertage"] == 2
    assert "zugesagt_liefertage_pro_shop" not in rows[0]


class StockConnection(Connection):
    def __init__(self):
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        return Result(
            many=[
                {
                    "id": 4,
                    "bezeichnung": "Servo",
                    "menge": 3,
                    "einheit": "Stk",
                    "aktualisiert_am": "2026-08-03T10:00:00Z",
                }
            ]
        )


def test_get_stock_returns_only_positive_stock_in_stable_order():
    repository = PostgresRepository("unused")
    connection = StockConnection()
    repository._connect = lambda: connection

    rows = repository.get_stock()

    assert "WHERE s.menge > 0" in connection.sql
    assert "ORDER BY lower(s.bezeichnung), s.id" in connection.sql
    for field in ("o.artikelnummer", "shop_name", "o.produkt_url"):
        assert field in connection.sql
    assert "LEFT JOIN purchase_item" in connection.sql
    assert rows[0]["bezeichnung"] == "Servo"


def test_get_stock_movements_returns_latest_entries_with_stock_name():
    repository = PostgresRepository("unused")
    connection = JobListConnection()
    repository._connect = lambda: connection

    repository.get_stock_bewegungen(20)

    assert "FROM stock_bewegung b JOIN stock s" in connection.sql
    assert "ORDER BY b.erstellt_am DESC, b.id DESC LIMIT %s" in connection.sql


class ArrivalConnection(Connection):
    def __init__(self, arrived=False):
        self.arrived = arrived
        self.statements = []

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if "FROM purchase WHERE id" in normalized:
            return Result(one={"id": 7, "angekommen_am": "now" if self.arrived else None})
        if normalized.startswith("UPDATE purchase SET angekommen_am"):
            self.arrived = True
            return Result(one={"id": 7, "angekommen_am": "now"})
        if "FROM purchase_item pi" in normalized:
            return Result(many=[{"id": 9, "line_id": 12, "menge": 3, "suchtext": "Servo"}])
        if normalized.startswith("INSERT INTO stock("):
            return Result(one={"id": 4})
        return Result()


def test_mark_purchase_arrived_writes_one_access_ledger_entry_and_is_idempotent():
    repository = PostgresRepository("unused")
    connection = ArrivalConnection()
    repository._connect = lambda: connection

    repository.mark_purchase_arrived(7)
    repository.mark_purchase_arrived(7)

    ledger = [entry for entry in connection.statements if entry[0].startswith("INSERT INTO stock_bewegung")]
    assert ledger == [(
        "INSERT INTO stock_bewegung(stock_id, line_id, delta, grund) VALUES (%s, %s, %s, 'zugang_lieferung')",
        (4, 12, 3),
    )]
    assert_stock_invariant(
        [{"id": 4, "menge": 3}], [{"stock_id": 4, "delta": 3}]
    )


class MarkStockConnection(Connection):
    def __init__(self, *, status="offen", is_test=False, stock=None):
        self.status = status
        self.is_test = is_test
        self.stock = stock if stock is not None else [{"id": 4, "menge": 5}]
        self.statements = []

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if "FROM bom_line bl JOIN job" in normalized:
            return Result(one={"suchtext": "Servo MG90S", "menge": 3,
                               "status": self.status, "is_test": self.is_test})
        if normalized.startswith("SELECT s.id") or normalized.startswith("SELECT id, menge FROM stock"):
            return Result(many=self.stock)
        if normalized.startswith("UPDATE bom_line"):
            return Result(one={"id": 12, "status": "bestand", "kommentar": None})
        return Result()


def test_mark_line_uses_article_and_trimmed_text_matching_and_writes_negative_ledger():
    repository = PostgresRepository("unused")
    connection = MarkStockConnection()
    repository._connect = lambda: connection

    repository.mark_line(12, "bestand", None)

    match_sql = next(sql for sql, _ in connection.statements if sql.startswith("SELECT s.id"))
    assert "quelle.artikelnummer IS NOT NULL" in match_sql
    assert "quelle.shop_id = ziel.shop_id" in match_sql
    assert "lower(btrim(s.bezeichnung)) = lower(btrim(CAST(%s AS TEXT)))" in match_sql
    assert "ORDER BY s.aktualisiert_am, s.id FOR UPDATE OF s" in match_sql
    assert any(
        sql.startswith("INSERT INTO stock_bewegung") and params == (4, 12, -3)
        for sql, params in connection.statements
    )
    assert_stock_invariant(
        [{"id": 4, "menge": 2}],
        [{"stock_id": 4, "delta": 5}, {"stock_id": 4, "delta": -3}],
    )


def test_mark_line_explicit_selection_and_guards_reject_without_stock_write():
    repository = PostgresRepository("unused")
    selected = MarkStockConnection(stock=[{"id": 7, "menge": 3}])
    repository._connect = lambda: selected
    repository.mark_line(12, "bestand", None, [7])
    assert any("id = ANY(%s)" in sql for sql, _ in selected.statements)

    for connection, message in [
        (MarkStockConnection(status="bestand"), "bereits"),
        (MarkStockConnection(is_test=True), "Testjobs"),
        (MarkStockConnection(stock=[]), "deckt"),
    ]:
        repository._connect = lambda connection=connection: connection
        with pytest.raises(ValueError, match=message):
            repository.mark_line(12, "bestand", None)
        assert not any(sql.startswith("UPDATE stock") for sql, _ in connection.statements)

    unknown = MarkStockConnection(stock=[])
    repository._connect = lambda: unknown
    with pytest.raises(ValueError, match="unbekannt oder leer"):
        repository.mark_line(12, "bestand", None, [999])


class CheckStockConnection(Connection):
    def __init__(self):
        self.calls = 0

    def execute(self, sql, params=None):
        self.calls += 1
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT id, suchtext"):
            return Result(one={"id": 12, "suchtext": "Servo MG90S", "menge": 8})
        if "CASE WHEN EXISTS" in normalized:
            return Result(many=[{
                "stock_id": 4, "bezeichnung": "MG90S Motor", "menge": 5,
                "aktualisiert_am": datetime(2026, 8, 1), "match": "artikelnummer",
            }])
        return Result(many=[
            {"stock_id": 4, "bezeichnung": "MG90S Motor", "menge": 5,
             "aktualisiert_am": datetime(2026, 8, 1)},
            *[
                {"stock_id": index, "bezeichnung": f"Servo MG90S {index}", "menge": 1,
                 "aktualisiert_am": datetime(2026, 8, index)}
                for index in range(5, 12)
            ],
            {"stock_id": 20, "bezeichnung": "Netzteil", "menge": 9,
             "aktualisiert_am": datetime(2026, 8, 20)},
        ])


def test_check_stock_separates_matches_candidates_limits_and_calculates_shortage():
    repository = PostgresRepository("unused")
    repository._connect = lambda: CheckStockConnection()

    result = repository.check_stock(12)

    assert result["gedeckt"] is False
    assert result["fehlmenge"] == 3
    assert result["treffer"][0]["match"] == "artikelnummer"
    assert len(result["kandidaten"]) == 5
    assert all("Servo" in row["bezeichnung"] for row in result["kandidaten"])


class CorrectionConnection(Connection):
    def __init__(self, stock=None):
        self.stock = stock
        self.statements = []

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT id, menge FROM stock"):
            return Result(one=self.stock)
        if normalized.startswith("UPDATE stock"):
            self.stock = {**self.stock, "menge": self.stock["menge"] + params[0]}
            return Result(one=self.stock)
        return Result()


def test_correct_stock_locks_updates_and_writes_correction_ledger_atomically():
    repository = PostgresRepository("unused")
    connection = CorrectionConnection({"id": 4, "menge": 5})
    repository._connect = lambda: connection

    result = repository.korrigiere_bestand(4, -2, "Inventur")

    assert result["menge"] == 3
    assert connection.statements[0] == (
        "SELECT id, menge FROM stock WHERE id = %s FOR UPDATE", (4,)
    )
    assert any(
        sql.startswith("INSERT INTO stock_bewegung") and params == (4, -2, "Inventur")
        for sql, params in connection.statements
    )
    assert_stock_invariant(
        [{"id": 4, "menge": 3}],
        [{"stock_id": 4, "delta": 5}, {"stock_id": 4, "delta": -2}],
    )


def test_correct_stock_rejects_unknown_or_negative_result_without_write():
    repository = PostgresRepository("unused")
    for stock, message in [(None, "unbekannt"), ({"id": 4, "menge": 1}, "negativ")]:
        connection = CorrectionConnection(stock)
        repository._connect = lambda connection=connection: connection
        with pytest.raises(ValueError, match=message):
            repository.korrigiere_bestand(4, -2, "Inventur")
        assert not any(sql.startswith("UPDATE stock") for sql, _ in connection.statements)


def test_shop_writes_original_shipping_currency_and_rate_evidence():
    class ShopConnection(Connection):
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.statements.append((normalized, params))
            return Result(one={"id": 8, **(params or {})})

    repository = PostgresRepository("unused")
    connection = ShopConnection()
    repository._connect = lambda: connection
    values = {
        "name": "Amazon.de", "url": "https://amazon.de", "domain": "amazon.de",
        "land": "DE", "lieferziel_id": 1,
        "versand_chf": "6.57", "gratis_ab_chf": "46.06",
        "mindestbestellwert_chf": None, "lieferzeit_default_tage": 5,
        "profil_quelle_url": "https://amazon.de/hilfe", "versand_text": "6,99 EUR",
        "versand_original": "6.99", "gratis_ab_original": "49.00",
        "mindestbestellwert_original": None, "versand_waehrung": "EUR",
        "versand_kurs": "0.94", "versand_kurs_am": date(2026, 8, 11),
        "versand_kurs_quelle": "https://api.frankfurter.app/latest",
    }

    repository.create_shop(**values)
    repository.update_shop_profile(8, **{key: value for key, value in values.items() if key not in {
        "name", "url", "domain", "land", "lieferziel_id"
    }})

    insert_sql, _ = connection.statements[0]
    update_sql, _ = connection.statements[1]
    for column in (
        "versand_original", "gratis_ab_original", "mindestbestellwert_original",
        "versand_waehrung", "versand_kurs", "versand_kurs_am", "versand_kurs_quelle",
    ):
        assert column in insert_sql
        assert column in update_sql


class OfferConnection:
    def __init__(self):
        self.statements = []
        self.current_day = date(2026, 8, 9)
        self.rows = {}
        self.next_id = 12

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if normalized.startswith("INSERT INTO offer"):
            assert "ON CONFLICT (line_id, produkt_url, beobachtungstag)" in normalized
            values = dict(params or {})
            key = (values["line_id"], values["produkt_url"], self.current_day)
            existing = self.rows.get(key)
            row_id = existing["id"] if existing else self.next_id
            if not existing:
                self.next_id += 1
            row = {"id": row_id, "beobachtungstag": self.current_day, **values}
            self.rows[key] = row
            return Result(one=row)
        return Result()


def offer_values(**changes):
    values = {
        "line_id": 1,
        "shop_id": 5,
        "produktname": "Servo",
        "produkt_url": "https://shop.ch/servo",
        "quelle_url": "https://shop.ch/servo",
        "preis_chf": "63.62",
        "lieferzeit_tage": 4,
        "lieferzeit_text": "3-4 Tage, bei Lieferant an Lager",
        "lager_text": "Filiale rot; CH-Lieferant an Lager",
        "lager": "Filiale rot; CH-Lieferant an Lager",
        "artikelnummer": "SKU-99",
        "provenienz_text": "Verkauf und Versand durch Amazon",
        "preis_original": "63.62",
        "waehrung": "CHF",
        "kurs": "1",
        "kurs_am": None,
        "kurs_quelle": None,
    }
    return {**values, **changes}


def test_create_offer_updates_same_day_but_inserts_new_daily_observation():
    repository = PostgresRepository("unused")
    connection = OfferConnection()
    repository._connect = lambda: connection

    first = repository.create_offer(**offer_values())
    corrected = repository.create_offer(**offer_values(preis_chf="60.00"))
    connection.current_day += timedelta(days=1)
    next_day = repository.create_offer(**offer_values(preis_chf="59.00"))

    insert_sql = connection.statements[0][0]
    assert "lieferzeit_text, lager_text" in insert_sql
    assert "provenienz_text" in insert_sql
    # Der sichtbare Verkäufer eines Marktplatzangebots überlebt eine
    # Auffrischung, die ihn nicht kennt - wie die Artikelnummer daneben.
    assert (
        "provenienz_text = COALESCE( EXCLUDED.provenienz_text, offer.provenienz_text )"
        in insert_sql
    )
    assert any(
        "status = 'kandidaten', kommentar = NULL" in sql
        for sql, _ in connection.statements
    )
    # Die Preishistorie fuehrt den Originalbetrag mit.
    assert "preis_original" in insert_sql
    assert first["id"] == corrected["id"]
    assert next_day["id"] != corrected["id"]
    assert len(connection.rows) == 2
    assert connection.rows[(1, "https://shop.ch/servo", date(2026, 8, 9))]["preis_chf"] == "60.00"
    assert connection.rows[(1, "https://shop.ch/servo", date(2026, 8, 10))]["preis_chf"] == "59.00"
